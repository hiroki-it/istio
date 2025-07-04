#!/usr/bin/python
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

import time
import asyncio
import logging
import os
import requests
import simplejson as json
import sys

from flask import Flask, request, session, render_template, redirect, g, url_for
from json2html import json2html
from opentelemetry import trace
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.b3 import B3MultiFormat
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import Counter, generate_latest
from authlib.integrations.flask_client import OAuth
from loguru import logger

# These two lines enable debugging at httplib level (requests->urllib3->http.client)
# You will see the REQUEST, including HEADERS and DATA, and RESPONSE with HEADERS but without DATA.
# The only thing missing will be the response.body which is not logged.
import http.client as http_client
http_client.HTTPConnection.debuglevel = 0

app = Flask(__name__)

oauth = OAuth(app)
oauth.register(
    name="keycloak",
    client_id="service",
    client_secret="ZQBzxI5CU36UiQmrWtDbJkY3VOX5LJRY",
    client_kwargs={"scope": "openid profile email"},
    authorize_url="http://localhost:8080/realms/dev/protocol/openid-connect/auth",
    access_token_url="http://keycloak-http.keycloak.svc.cluster.local:8080/realms/dev/protocol/openid-connect/token",
    jwks_uri="http://keycloak-http.keycloak.svc.cluster.local:8080/realms/dev/protocol/openid-connect/certs"
)

# loguruの設定
logger.remove() 
logger.add(
    sys.stdout,
    format="{time} {level} {extra[trace_id]} {message}", 
    # 構造化ログ
    serialize=True 
)

# Set the secret key to some random bytes. Keep this really secret!
app.secret_key = b'_5#y2L"F4Q8z\n\xec]/'

servicesDomain = "" if (os.environ.get("SERVICES_DOMAIN") is None) else "." + os.environ.get("SERVICES_DOMAIN")
detailsHostname = "details" if (os.environ.get("DETAILS_HOSTNAME") is None) else os.environ.get("DETAILS_HOSTNAME")
detailsPort = "9080" if (os.environ.get("DETAILS_SERVICE_PORT") is None) else os.environ.get("DETAILS_SERVICE_PORT")
ratingsHostname = "ratings" if (os.environ.get("RATINGS_HOSTNAME") is None) else os.environ.get("RATINGS_HOSTNAME")
ratingsPort = "9080" if (os.environ.get("RATINGS_SERVICE_PORT") is None) else os.environ.get("RATINGS_SERVICE_PORT")
reviewsHostname = "reviews" if (os.environ.get("REVIEWS_HOSTNAME") is None) else os.environ.get("REVIEWS_HOSTNAME")
reviewsPort = "9080" if (os.environ.get("REVIEWS_SERVICE_PORT") is None) else os.environ.get("REVIEWS_SERVICE_PORT")

flood_factor = 0 if (os.environ.get("FLOOD_FACTOR") is None) else int(os.environ.get("FLOOD_FACTOR"))

details = {
    "name": "http://{0}{1}:{2}".format(detailsHostname, servicesDomain, detailsPort),
    "endpoint": "details",
    "children": []
}

ratings = {
    "name": "http://{0}{1}:{2}".format(ratingsHostname, servicesDomain, ratingsPort),
    "endpoint": "ratings",
    "children": []
}

reviews = {
    "name": "http://{0}{1}:{2}".format(reviewsHostname, servicesDomain, reviewsPort),
    "endpoint": "reviews",
    "children": [ratings]
}

productpage = {
    "name": "http://{0}{1}:{2}".format(detailsHostname, servicesDomain, detailsPort),
    "endpoint": "details",
    "children": [details, reviews]
}

service_dict = {
    "productpage": productpage,
    "details": details,
    "reviews": reviews,
}

request_result_counter = Counter('request_result', 'Results of requests', ['destination_app', 'response_code'])

# A note on distributed tracing:
#
# Although Istio proxies are able to automatically send spans, they need some
# hints to tie together the entire trace. Applications need to propagate the
# appropriate HTTP headers so that when the proxies send span information, the
# spans can be correlated correctly into a single trace.
#
# To do this, an application needs to collect and propagate headers from the
# incoming request to any outgoing requests. The choice of headers to propagate
# is determined by the trace configuration used. See getForwardHeaders for
# the different header options.
#
# This example code uses OpenTelemetry (http://opentelemetry.io/) to propagate
# the 'b3' (zipkin) headers. Using OpenTelemetry for this is not a requirement.
# Using OpenTelemetry allows you to add application-specific tracing later on,
# but you can just manually forward the headers if you prefer.
#
# The OpenTelemetry example here is very basic. It only forwards headers. It is
# intended as a reference to help people get started, eg how to create spans,
# extract/inject context, etc.


propagator = B3MultiFormat()
set_global_textmap(B3MultiFormat())
provider = TracerProvider()
# Sets the global default tracer provider
trace.set_tracer_provider(provider)

tracer = trace.get_tracer(__name__)


def getForwardHeaders(request):
    headers = {}

    # x-b3-*** headers can be populated using the OpenTelemetry span
    ctx = propagator.extract(carrier={k.lower(): v for k, v in request.headers})
    propagator.inject(headers, ctx)

    # We handle other (non x-b3-***) headers manually
    if 'user' in session:
        headers['end-user'] = session['user']

    # Keep this in sync with the headers in details and reviews.
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
        # This is handled by opentelemetry above
        # 'x-b3-traceid',
        # 'x-b3-spanid',
        # 'x-b3-parentspanid',
        # 'x-b3-sampled',
        # 'x-b3-flags',

        # SkyWalking trace headers.
        'sw8',

        # Application-specific headers to forward.
        'user-agent',

        # Context and session specific headers
        'cookie',
        'authorization',
        'jwt',
    ]
    # For Zipkin, always propagate b3 headers.
    # For Lightstep, always propagate the x-ot-span-context header.
    # For Datadog, propagate the corresponding datadog headers.
    # For OpenCensusAgent and Stackdriver configurations, you can choose any
    # set of compatible headers to propagate within your application. For
    # example, you can propagate b3 headers or W3C trace context headers with
    # the same result. This can also allow you to translate between context
    # propagation mechanisms between different applications.

    for ihdr in incoming_headers:
        val = request.headers.get(ihdr)
        if val is not None:
            headers[ihdr] = val

    return headers


# The UI:
@app.route('/')
@app.route('/index.html')
def index():
    """ Display productpage with normal user and test user buttons"""
    global productpage

    table = json2html.convert(json=json.dumps(productpage),
                              table_attributes="class=\"table table-condensed table-bordered table-hover\"")

    return render_template('index.html', serviceTable=table)


@app.route('/health')
def health():
    return 'Product page is healthy'

@app.route('/login')
def login():
    logger.bind(trace_id=get_trace_id()).info("Start to login")
    redirect_uri = url_for("callback", _external=True)
    redirectResponse = oauth.keycloak.authorize_redirect(redirect_uri)
    return redirectResponse

@app.route("/callback")
def callback():
    logger.bind(trace_id=get_trace_id()).info("Start to callback")
    response = app.make_response(redirect(url_for('front', _external=True)))

    try:
      # 各種トークンを取得する
      token = oauth.keycloak.authorize_access_token()
      session['id_token'] = token['id_token']
      # デコードしたIDトークンを取得する
      id_token = oauth.keycloak.parse_id_token(token, None)
      session['user'] = id_token['given_name']
      # Cookieヘッダーにアクセストークンを設定する
      response.set_cookie('access_token', token['access_token'])
    except BaseException as e:
      logger.bind(trace_id=get_trace_id()).error(f"{repr(e)}")
    
    return response

@app.route('/logout')
def logout():

    # LOGGED_INのフラグを有効化した場合、ログアウトを無効にする
    if os.getenv('LOGGED_IN', 'False') == 'True':
        return app.make_response(redirect(url_for('front', _external=True)))

    logger.bind(trace_id=get_trace_id()).info("Start to logout")
    # Keycloakからログアウトし、productpageにリダイレクトする
    redirect_uri = ("http://localhost:8080/realms/dev/protocol/openid-connect/logout?id_token_hint=%s&post_logout_redirect_uri=%s" % (session.get('id_token', ''), url_for("front", _external=True)))
    session.pop('id_token', None)
    session.pop('user', None)
    response = app.make_response(redirect(redirect_uri))
    # Cookieヘッダーのアクセストークンを削除する
    response.delete_cookie('access_token')
    return response

# a helper function for asyncio.gather, does not return a value


async def getProductReviewsIgnoreResponse(product_id, headers):
    getProductReviews(product_id, headers)

# flood reviews with unnecessary requests to demonstrate Istio rate limiting, asynchoronously


async def floodReviewsAsynchronously(product_id, headers):
    # the response is disregarded
    await asyncio.gather(*(getProductReviewsIgnoreResponse(product_id, headers) for _ in range(flood_factor)))

# flood reviews with unnecessary requests to demonstrate Istio rate limiting


def floodReviews(product_id, headers):
    loop = asyncio.new_event_loop()
    loop.run_until_complete(floodReviewsAsynchronously(product_id, headers))
    loop.close()

# フロントエンドアプリとして
@app.route('/productpage')
def front():
    product_id = 0  # TODO: replace default value
    
    # LOGGED_INのフラグを有効化した場合、最初からログイン済みにする
    if os.getenv('LOGGED_IN', 'False') == 'True':
        session['user'] = 'izzy'

    headers = getForwardHeaders(request)
    user = session.get('user', '')
    product = getProduct(product_id)

    # detailsサービスにリクエストを送信する
    detailsStatus, details = getProductDetails(product_id, headers)
    logger.bind(trace_id=get_trace_id()).info("[" + str(detailsStatus) + "] Details response is " + str(details))

    if flood_factor > 0:
        floodReviews(product_id, headers)

    # reviewsサービスにリクエストを送信する
    reviewsStatus, reviews = getProductReviews(product_id, headers)
    logger.bind(trace_id=get_trace_id()).info("[" + str(reviewsStatus) + "] Reviews response is " + str(reviews))

    # いずれかのマイクロサービスでアクセストークンの検証が失敗し、401ステータスが返信された場合、ログアウトする
    if detailsStatus == 401 or reviewsStatus == 401:
        logger.bind(trace_id=get_trace_id()).info("[" + str(401) + "] Access token is invalid")
        redirect_uri = url_for('logout', _external=True)
        return redirect(redirect_uri)

    response = app.make_response(render_template(
        'productpage.html',
        detailsStatus=detailsStatus,
        reviewsStatus=reviewsStatus,
        product=product,
        details=details,
        reviews=reviews,
        user=user))

    return response

# API Gatewayとして
@app.route('/api/v1/products')
def productsRoute():
    return json.dumps(getProducts()), 200, {'Content-Type': 'application/json'}


@app.route('/api/v1/products/<product_id>')
def productRoute(product_id):
    headers = getForwardHeaders(request)
    status, details = getProductDetails(product_id, headers)
    return json.dumps(details), status, {'Content-Type': 'application/json'}


@app.route('/api/v1/products/<product_id>/reviews')
def reviewsRoute(product_id):
    headers = getForwardHeaders(request)
    status, reviews = getProductReviews(product_id, headers)
    return json.dumps(reviews), status, {'Content-Type': 'application/json'}


@app.route('/api/v1/products/<product_id>/ratings')
def ratingsRoute(product_id):
    headers = getForwardHeaders(request)
    status, ratings = getProductRatings(product_id, headers)
    return json.dumps(ratings), status, {'Content-Type': 'application/json'}


@app.route('/metrics')
def metrics():
    return generate_latest()


# Data providers:
def getProducts():
    return [
        {
            'id': 0,
            'title': 'The Comedy of Errors',
            'descriptionHtml': '<a href="https://en.wikipedia.org/wiki/The_Comedy_of_Errors">Wikipedia Summary</a>: The Comedy of Errors is one of <b>William Shakespeare\'s</b> early plays. It is his shortest and one of his most farcical comedies, with a major part of the humour coming from slapstick and mistaken identity, in addition to puns and word play.'
        }
    ]


def getProduct(product_id):
    products = getProducts()
    if product_id + 1 > len(products):
        return None
    else:
        return products[product_id]


def getProductDetails(product_id, headers):
    try:
        url = details['name'] + "/" + details['endpoint'] + "/" + str(product_id)
        res = send_request(url, headers=headers, timeout=3.0)
    except BaseException as e:
        logger.bind(trace_id=get_trace_id()).error(f"{repr(e)}")
        res = None
    if res and res.status_code == 200:
        request_result_counter.labels(destination_app='details', response_code=res.status_code).inc()
        return res.status_code, res.json()
    elif res is not None and res.status_code == 403:
        request_result_counter.labels(destination_app='details', response_code=res.status_code).inc()
        return res.status_code, {'error': 'Please sign in to view product details.'}
    elif res is not None and (res.status_code == 503  or res.status_code == 504):
        request_result_counter.labels(destination_app='details', response_code=res.status_code).inc()
        try:
          return res.status_code, res.json()
        except BaseException as e:
          logger.bind(trace_id=get_trace_id()).error(f"{repr(e)}")
          # detailsサービスが503または504ステータスでJSONデータがない場合
          return res.status_code, {'error': 'Sorry, product details are currently unavailable.'}
    else:
        status = res.status_code if res is not None and res.status_code else 500
        request_result_counter.labels(destination_app='details', response_code=status).inc()
        return status, {'error': 'Sorry, product details are currently unavailable.'}


def getProductReviews(product_id, headers):
    try:
        url = reviews['name'] + "/" + reviews['endpoint'] + "/" + str(product_id)
        res = send_request(url, headers=headers, timeout=3.0)
    except BaseException as e:
        logger.bind(trace_id=get_trace_id()).error(f"{repr(e)}")
        res = None
    if res and res.status_code == 200:
        request_result_counter.labels(destination_app='reviews', response_code=res.status_code).inc()
        return res.status_code, res.json()
    elif res is not None and res.status_code == 403:
        request_result_counter.labels(destination_app='reviews', response_code=res.status_code).inc()
        return res.status_code, {'error': 'Please sign in to view product reviews.'}
    elif res is not None and (res.status_code == 503  or res.status_code == 504):
        request_result_counter.labels(destination_app='reviews', response_code=res.status_code).inc()
        try:
          return res.status_code, res.json()
        except BaseException as e:
          logger.bind(trace_id=get_trace_id()).error(f"{repr(e)}")
          # reviewsサービスが503または504ステータスでJSONデータがない場合
          return res.status_code, {'error': 'Sorry, product reviews are currently unavailable.'}
    else:
        status = res.status_code if res is not None and res.status_code else 500
        request_result_counter.labels(destination_app='reviews', response_code=status).inc()
        return status, {'error': 'Sorry, product reviews are currently unavailable.'}


def getProductRatings(product_id, headers):
    try:
        url = ratings['name'] + "/" + ratings['endpoint'] + "/" + str(product_id)
        res = send_request(url, headers=headers, timeout=3.0)
    except BaseException as e:
        logger.bind(trace_id=get_trace_id()).error(f"{repr(e)}")
        res = None
    if res and res.status_code == 200:
        request_result_counter.labels(destination_app='ratings', response_code=res.status_code).inc()
        return res.status_code, res.json()
    elif res is not None and res.status_code == 403:
        request_result_counter.labels(destination_app='ratings', response_code=res.status_code).inc()
        return res.status_code, {'error': 'Please sign in to view product ratings.'}
    elif res is not None and (res.status_code == 503  or res.status_code == 504):
        request_result_counter.labels(destination_app='ratings', response_code=res.status_code).inc()
        try:
          return res.status_code, res.json()
        except BaseException as e:
          logger.bind(trace_id=get_trace_id()).error(f"{repr(e)}")
          # ratingsサービスが503または504ステータスでJSONデータがない場合
          return res.status_code, {'error': 'Sorry, product ratings are currently unavailable.'}
    else:
        status = res.status_code if res is not None and res.status_code else 500
        request_result_counter.labels(destination_app='ratings', response_code=status).inc()
        return status, {'error': 'Sorry, product ratings are currently unavailable.'}


def send_request(url, **kwargs):
    # We intentionally do not pool so that we can easily test load distribution across many versions of our backends
    return requests.get(url, **kwargs)


def get_trace_id():
    # Envoyの作成したtraceparent値を取得する
    traceparent = request.headers.get("traceparent")
    if traceparent:
        # W3C Trace Context
        # traceparent: 00-<trace_id>-<span_id>-01
        parts = traceparent.split("-")
        if len(parts) >= 2:
            return parts[1] 
    return "unknown"

class Writer(object):
    def __init__(self, filename):
        self.file = open(filename, 'w')

    def write(self, data):
        self.file.write(data)

    def flush(self):
        self.file.flush()

if __name__ == '__main__':
    if len(sys.argv) < 2:
        logger.error("Usage: %s port" % (sys.argv[0]))
        sys.exit(-1)

    p = int(sys.argv[1])
    logger.info("Start at port %s" % (p))
    # Make it compatible with IPv6 if Linux
    if sys.platform == "linux":
        app.run(host='::', port=p, debug=False, threaded=True)
    else:
        app.run(host='0.0.0.0', port=p, debug=False, threaded=True)
