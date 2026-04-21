const grpc = require('@grpc/grpc-js');
const protoLoader = require('@grpc/proto-loader');
const parseArgs = require('minimist');
const fs = require('fs');
const { generateSelfSignedCert } = require('./generate-cert');

const PROTO_PATH = __dirname + '/proto/echo.proto';

const packageDefinition = protoLoader.loadSync(
  PROTO_PATH,
  {keepCase: true,
   longs: String,
   enums: String,
   defaults: true,
   oneofs: true
  });
const echoProto = grpc.loadPackageDefinition(packageDefinition).grpc.examples.echo;

function unaryEcho(call, callback) {
  callback(null, call.request);
}

function bidirectionalStreamingEcho(call) {
  call.on('data', request => {
    call.write(request);
  });
  call.on('end', () => {
    call.end();
  });
}

const serviceImplementation = {
  unaryEcho,
  bidirectionalStreamingEcho
}

// logger is to mock a sophisticated logging system. To simplify the example, we just print out the content.
function logger(format, ...args) {
  console.log(`LOG (server):\t${format}\n`, ...args);
}

function loggingInterceptor(methodDescriptor, call) {
  const listener = new grpc.ServerListenerBuilder()
    .withOnReceiveMessage((message, next) => {
      logger(`Receive a message ${JSON.stringify(message)} at ${(new Date()).toISOString()}`);
      next(message);
    }).build();
  const responder = new grpc.ResponderBuilder()
    .withStart(next => {
      next(listener);
    })
    .withSendMessage((message, next) => {
      logger(`Send a message ${JSON.stringify(message)} at ${(new Date()).toISOString()}`);
      next(message);
    }).build();
  return new grpc.ServerInterceptingCall(call, responder);
}

function main() {
  const argv = parseArgs(process.argv.slice(2), {
    string: 'port',
    default: {port: '50051'}
  });
  const server = new grpc.Server({interceptors: [loggingInterceptor]});
  server.addService(echoProto.Echo.service, serviceImplementation);
  if (!fs.existsSync('server-cert.pem') || !fs.existsSync('server-key.pem')) {
    console.log('Generating self-signed certificate...');
    generateSelfSignedCert();
  }
  
  const serverCert = fs.readFileSync('server-cert.pem');
  const serverKey = fs.readFileSync('server-key.pem');
  
  const credentials = grpc.ServerCredentials.createSsl(null, [{
    cert_chain: serverCert,
    private_key: serverKey
  }]);
  
  server.bindAsync(`0.0.0.0:${argv.port}`, credentials, (err, port) => {
    if (err != null) {
      return console.error(err);
    }
    console.log(`gRPC listening on ${port}`)
  });
}

main();