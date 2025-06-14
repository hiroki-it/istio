// Copyright Istio Authors
//
//   Licensed under the Apache License, Version 2.0 (the "License");
//   you may not use this file except in compliance with the License.
//   You may obtain a copy of the License at
//
//       http://www.apache.org/licenses/LICENSE-2.0
//
//   Unless required by applicable law or agreed to in writing, software
//   distributed under the License is distributed on an "AS IS" BASIS,
//   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
//   See the License for the specific language governing permissions and
//   limitations under the License.

var http = require('http')
var dispatcher = require('httpdispatcher')
const pino = require('pino');

let logger = pino();
var port = parseInt(process.argv[2])
var userAddedRatings = [] // used to demonstrate POST functionality
var unavailable = false
var healthy = true

if (process.env.SERVICE_VERSION === 'v-unavailable') {
    // make the service unavailable once in 60 seconds
    setInterval(function () {
        unavailable = !unavailable
    }, 60000);
}

if (process.env.SERVICE_VERSION === 'v-unhealthy') {
    // make the service unavailable once in 15 minutes for 15 minutes.
    // 15 minutes is chosen since the Kubernetes's exponential back-off is reset after 10 minutes
    // of successful execution
    // see https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#restart-policy
    // Kiali shows the last 10 or 30 minutes, so to show the error rate of 50%,
    // it will be required to run the service for 30 minutes, 15 minutes of each state (healthy/unhealthy)
    setInterval(function () {
        healthy = !healthy
        unavailable = !unavailable
    }, 900000);
}

/**
 * We default to using mongodb, if DB_TYPE is not set to mysql.
 */
if (process.env.SERVICE_VERSION === 'v2' || process.env.SERVICE_VERSION === 'v3') {
  if (process.env.DB_TYPE === 'mysql') {
    var mysql = require('mysql')
    var hostName = process.env.MYSQL_DB_HOST
    var portNumber = process.env.MYSQL_DB_PORT
    var username = process.env.MYSQL_DB_USER
    var password = process.env.MYSQL_DB_PASSWORD
  } else {
    var MongoClient = require('mongodb').MongoClient
    var url = process.env.MONGO_DB_URL
  }
}

dispatcher.onPost(/^\/ratings\/[0-9]*/, function (req, res) {
  var productIdStr = req.url.split('/').pop()
  var productId = parseInt(productIdStr)
  var ratings = {}

  if (Number.isNaN(productId)) {
    res.writeHead(400, {'Content-type': 'application/json'})
    res.end(JSON.stringify({error: 'please provide numeric product ID'}))
    return
  }

  try {
    ratings = JSON.parse(req.body)
  } catch (error) {
    res.writeHead(400, {'Content-type': 'application/json'})
    res.end(JSON.stringify({error: 'please provide valid ratings JSON'}))
    return
  }

  if (process.env.SERVICE_VERSION === 'v2' || process.env.SERVICE_VERSION === 'v3') { // the version that is backed by a database
    res.writeHead(501, {'Content-type': 'application/json'})
    res.end(JSON.stringify({error: 'Post not implemented for database backed ratings'}))
  } else { // the version that holds ratings in-memory
    res.writeHead(200, {'Content-type': 'application/json'})
    res.end(JSON.stringify(putLocalReviews(productId, ratings)))
  }
})

dispatcher.onGet(/^\/ratings\/[0-9]*/, function (req, res) {
  var productIdStr = req.url.split('/').pop()
  var productId = parseInt(productIdStr)

  if (Number.isNaN(productId)) {
    logger.error({trace_id: getTraceId(req.headers)}, 'Please provide numeric product ID')
    res.writeHead(400, {'Content-type': 'application/json'})
    res.end(JSON.stringify({error: 'Please provide numeric product ID'}))
  } else if (process.env.SERVICE_VERSION === 'v2' || process.env.SERVICE_VERSION === 'v3') {
    var firstRating = 0
    var secondRating = 0

    if (process.env.DB_TYPE === 'mysql') {
      var connection = mysql.createConnection({
        host: hostName,
        port: portNumber,
        user: username,
        password: password,
        database: 'test'
      })

      connection.connect(function(err) {
          if (err) {
              logger.error({trace_id: getTraceId(req.headers)}, err)
              res.writeHead(500, {'Content-type': 'application/json'})
              res.end(JSON.stringify({error: 'could not connect to ratings database'}))
              return
          }
          connection.query('SELECT Rating FROM ratings', function (err, results, fields) {
              if (err) {
                  logger.error({trace_id: getTraceId(req.headers)}, err)
                  res.writeHead(500, {'Content-type': 'application/json'})
                  res.end(JSON.stringify({error: 'could not perform select'}))
              } else {
                  if (results[0]) {
                      firstRating = results[0].Rating
                  }
                  if (results[1]) {
                      secondRating = results[1].Rating
                  }
                  var result = {
                      id: productId,
                      ratings: {
                          Reviewer1: firstRating,
                          Reviewer2: secondRating
                      }
                  }
                  logger.info({trace_id: getTraceId(req.headers)}, "Get rating successfully")
                  res.writeHead(200, {'Content-type': 'application/json'})
                  res.end(JSON.stringify(result))
              }
          })
          // close the connection
          connection.end()
      })
    } else {
      MongoClient.connect(url, function (err, client) {
        if (err) {
          logger.error({trace_id: getTraceId(req.headers)}, err)
          res.writeHead(500, {'Content-type': 'application/json'})
          res.end(JSON.stringify({error: 'Could not connect to ratings database'}))
        } else {
          const db = client.db("test")
          db.collection('ratings').find({}).toArray(function (err, data) {
            if (err) {
              logger.error({trace_id: getTraceId(req.headers)}, err)
              res.writeHead(500, {'Content-type': 'application/json'})
              res.end(JSON.stringify({error: 'could not load ratings from database'}))
            } else {
              if (data[0]) {
                firstRating = data[0].rating
              }
              if (data[1]) {
                secondRating = data[1].rating
              }
              var result = {
                id: productId,
                ratings: {
                  Reviewer1: firstRating,
                  Reviewer2: secondRating
                }
              }
              logger.info({trace_id: getTraceId(req.headers)}, "Get rating successfully")
              res.writeHead(200, {'Content-type': 'application/json'})
              res.end(JSON.stringify(result))
            }
            // close client once done:
            client.close()
          })
        }
      })
    }
  } else {
      if (process.env.SERVICE_VERSION === 'v-faulty') {
        // in half of the cases return error,
        // in another half proceed as usual
        var random = Math.random(); // returns [0,1]
        if (random <= 0.5) {
          getLocalReviewsServiceUnavailable(res)
        } else {
          getLocalReviewsSuccessful(res, productId)
        }
      }
      else if (process.env.SERVICE_VERSION === 'v-delayed') {
        // in half of the cases delay for 7 seconds,
        // in another half proceed as usual
        var random = Math.random(); // returns [0,1]
        if (random <= 0.5) {
          setTimeout(getLocalReviewsSuccessful, 7000, res, productId)
        } else {
          getLocalReviewsSuccessful(res, productId)
        }
      }
      else if (process.env.SERVICE_VERSION === 'v-unavailable' || process.env.SERVICE_VERSION === 'v-unhealthy') {
          if (unavailable) {
              getLocalReviewsServiceUnavailable(res)
          } else {
              getLocalReviewsSuccessful(res, productId)
          }
      }
      else if (process.env.SERVICE_VERSION === 'v-50percent-internal-server-error-500') {
          // 50%の確率で、500ステータスを返信する
          var random = Math.random();
          if (random <= 0.5) {
              getLocalReviewsInternalServerError(res)
          } else {
              getLocalReviewsSuccessful(res, productId)
          }
      }
      else {
        getLocalReviewsSuccessful(res, productId)
      }
  }
})

dispatcher.onGet('/health', function (req, res) {
    if (healthy) {
        res.writeHead(200, {'Content-type': 'application/json'})
        res.end(JSON.stringify({status: 'Ratings is healthy'}))
    } else {
        res.writeHead(500, {'Content-type': 'application/json'})
        res.end(JSON.stringify({status: 'Ratings is not healthy'}))
    }
})

function putLocalReviews (productId, ratings) {
  userAddedRatings[productId] = {
    id: productId,
    ratings: ratings
  }
  return getLocalReviews(productId)
}

function getLocalReviewsSuccessful(res, productId) {
  res.writeHead(200, {'Content-type': 'application/json'})
  res.end(JSON.stringify(getLocalReviews(productId)))
}

function getLocalReviewsServiceUnavailable(res) {
  res.writeHead(503, {'Content-type': 'application/json'})
  res.end(JSON.stringify({error: 'Service unavailable'}))
}

function getLocalReviewsInternalServerError(res) {
  res.writeHead(500, {'Content-type': 'application/json'})
  res.end(JSON.stringify({error: 'Internal Server Error'}))
}

function getLocalReviews (productId) {
  if (typeof userAddedRatings[productId] !== 'undefined') {
      return userAddedRatings[productId]
  }
  return {
    id: productId,
    ratings: {
      'Reviewer1': 5,
      'Reviewer2': 4
    }
  }
}

function getTraceId(headers) {
  // Envoyの作成したtraceparent値を取得する
  const traceparent = headers?.['traceparent'];
  if (traceparent) {
    // W3C Trace Context
    // traceparent: 00-<trace_id>-<span_id>-01
    const parts = traceparent.split('-');
    if (parts.length >= 2) {
      return parts[1];
    }
  }
  return 'unknown';
}

function handleRequest (req, res) {

  try {
    // ヘルスチェックはロギングしない
    if (req.url != '/health') {
      logger.info({trace_id: getTraceId(req.headers)}, req.method + ' ' + req.url)
    }
    dispatcher.dispatch(req, res)
  } catch (err) {
    if (req.url != '/health') {
      logger.error({trace_id: getTraceId(req.headers)}, err)
    }
  }
}

var server = http.createServer(handleRequest)

process.on('SIGTERM', function () {
  logger.info("SIGTERM received")
  server.close(function () {
    process.exit(0);
  });
});

server.listen(port, function () {
  logger.info('Server listening on: http://0.0.0.0:' + port)
})
