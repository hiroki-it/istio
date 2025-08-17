#!/usr/bin/ruby
#
# Copyright Istio Authors
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

require 'webrick'
require 'json'
require 'net/http'
require 'semantic_logger'

# ログをメモリ上に保管せずにフラッシュする
$stdout.sync = true

SemanticLogger.add_appender(
  io: $stdout,
  formatter: -> log, logger {
    # 必要なフィールドのみを設定する
    record = {
      # タイムスタンプの形式を変更する
      "timestamp" => log.time.utc.iso8601(6),  
      "level"     => log.level,
      "message"   => log.message,
    }

    # payloadフィールドを展開する
    if log.payload.is_a?(Hash)
      record.merge!(log.payload.transform_keys!(&:to_s))
    end

    JSON.generate(record)
  }
)

logger = SemanticLogger['Details']

if ARGV.length < 1 then
    puts "usage: #{$PROGRAM_NAME} port"
    exit(-1)
end

port = Integer(ARGV[0])

logger.info("Start at port #{port}")

server = WEBrick::HTTPServer.new(
    :BindAddress => '*',
    :Port => port,
    :AcceptCallback => -> (s) { s.setsockopt(Socket::IPPROTO_TCP, Socket::TCP_NODELAY, 1) },
    :Logger => logger,
    :AccessLog => []
)

trap 'INT' do
  logger.info("Shutting down server")
  server.shutdown
end

server.mount_proc '/health' do |req, res|
    res.status = 200
    res.body = {'status' => 'Details is healthy'}.to_json
    res['Content-Type'] = 'application/json'
end

server.mount_proc '/details' do |req, res|
    pathParts = req.path.split('/')
    headers = get_forward_headers(req)
    trace_id = get_trace_id(headers)

    begin
        begin
          id = Integer(pathParts[-1])
        rescue
          raise 'please provide numeric product id'
        end
        details = get_book_details(id, headers)
        res.body = details.to_json
        res['Content-Type'] = 'application/json'
    rescue => error
        logger.error("Failed to get book details: #{error.message}", trace_id: trace_id)
        res.body = {'error' => error.message}.to_json
        res['Content-Type'] = 'application/json'
        res.status = 500
    end
end

# TODO: provide details on different books.
def get_book_details(id, headers)
    if ENV['ENABLE_EXTERNAL_BOOK_SERVICE'] === 'true' then
      # the ISBN of one of Comedy of Errors on the Amazon
      # that has Shakespeare as the single author
        isbn = '0486424618'
        return fetch_details_from_external_service(isbn, id, headers)
    end

    return {
        'id' => id,
        'author': 'William Shakespeare',
        'year': 1595,
        'type' => 'paperback',
        'pages' => 200,
        'publisher' => 'PublisherA',
        'language' => 'English',
        'ISBN-10' => '1234567890',
        'ISBN-13' => '123-1234567890'
    }
end

def fetch_details_from_external_service(isbn, id, headers)
    uri = URI.parse('https://www.googleapis.com/books/v1/volumes?q=isbn:' + isbn)
    http = Net::HTTP.new(uri.host, ENV['DO_NOT_ENCRYPT'] === 'true' ? 80:443)
    http.read_timeout = 5 # seconds

    # DO_NOT_ENCRYPT is used to configure the details service to use either
    # HTTP (true) or HTTPS (false, default) when calling the external service to
    # retrieve the book information.
    #
    # Unless this environment variable is set to true, the app will use TLS (HTTPS)
    # to access external services.
    unless ENV['DO_NOT_ENCRYPT'] === 'true' then
      http.use_ssl = true
    end

    request = Net::HTTP::Get.new(uri.request_uri)
    headers.each { |header, value| request[header] = value }
    trace_id = get_trace_id(headers)

    begin
      response = http.request(request)
      status_code = response.code.to_i
    rescue => error
      logger.error("Failed to get book details: #{error.message}", status: status_code, trace_id: trace_id
      return {}
    end
    
    if status_code >= 200 && status_code < 300
      json = JSON.parse(response.body)
      book = json['items'][0]['volumeInfo']
      language = book['language'] === 'en'? 'English' : 'unknown'
      type = book['printType'] === 'BOOK'? 'paperback' : 'unknown'
      isbn10 = get_isbn(book, 'ISBN_10')
      isbn13 = get_isbn(book, 'ISBN_13')
      logger.info("Get book details successfully", method: request.method, path: request.path, status: status_code, trace_id: trace_id)
      return {
        'id' => id,
        'author': book['authors'][0],
        'year': book['publishedDate'],
        'type' => type,
        'pages' => book['pageCount'],
        'publisher' => book['publisher'],
        'language' => language,
        'ISBN-10' => isbn10,
        'ISBN-13' => isbn13
      }
    elsif status_code == 404
      logger.info("Book details is not found", method: request.method, path: request.path, status: status_code, trace_id: trace_id)
      return {}
    elsif status_code >= 500
      logger.error("Failed to get book details", method: request.method, path: request.path, status: status_code, trace_id: trace_id)
      return {}
    else
      logger.error("Failed to get book details", method: request.method, path: request.path, status: status_code, trace_id: trace_id)
      return {}
    end
end

def get_isbn(book, isbn_type)
  isbn_dentifiers = book['industryIdentifiers'].select do |identifier|
    identifier['type'] === isbn_type
  end

  return isbn_dentifiers[0]['identifier']
end

def get_trace_id(headers)
  
  # Envoyの作成したtraceparent値を取得する
  traceparent = headers['traceparent']

  if traceparent
    # W3C Trace Context
    # traceparent: 00-<trace_id>-<span_id>-01
    parts = traceparent.split('-')
    if parts.length >= 2
      return parts[1] 
    end
  end

  return 'unknown'
end

def get_forward_headers(request)
  headers = {}

  # Keep this in sync with the headers in productpage and reviews.
  incoming_headers = [
      # All applications should propagate x-request-id. This header is
      # included in access log statements and is used for consistent trace
      # sampling and log sampling decisions in Istio.
      'x-request-id',

      # Lightstep tracing header. Propagate this if you use lightstep tracing
      # in Istio (see
      # https://istio.io/latest/docs/tasks/observability/distributed-tracing/lightstep/)
      # Note: this should probably be changed to use B3 or W3C TRACE_CONTEXT.
      # Lightstep recommends using B3 or TRACE_CONTEXT and most application
      # libraries from lightstep do not support x-ot-span-context.
      'x-ot-span-context',

      # Datadog tracing header. Propagate these headers if you use Datadog
      # tracing.
      'x-datadog-trace-id',
      'x-datadog-parent-id',
      'x-datadog-sampling-priority',

      # W3C Trace Context. Compatible with OpenCensusAgent and Stackdriver Istio
      # configurations.
      'traceparent',
      'tracestate',

      # Cloud trace context. Compatible with OpenCensusAgent and Stackdriver Istio
      # configurations.
      'x-cloud-trace-context',

      # Grpc binary trace context. Compatible with OpenCensusAgent nad
      # Stackdriver Istio configurations.
      'grpc-trace-bin',

      # b3 trace headers. Compatible with Zipkin, OpenCensusAgent, and
      # Stackdriver Istio configurations.
      'x-b3-traceid',
      'x-b3-spanid',
      'x-b3-parentspanid',
      'x-b3-sampled',
      'x-b3-flags',

      # SkyWalking trace headers.
      'sw8',

      # Application-specific headers to forward.
      'end-user',
      'user-agent',

      # Context and session specific headers
      'cookie',
      'authorization',
      'jwt'
  ]

  request.each do |header, value|
    if incoming_headers.include? header then
      headers[header] = value
    end
  end

  return headers
end

server.start
