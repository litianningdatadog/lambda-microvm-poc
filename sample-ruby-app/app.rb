#!/usr/bin/env ruby
# frozen_string_literal: true

# Sample guest application that implements Lambda MicroVMs lifecycle hooks.
#
# Uses only the Ruby standard library (no external dependencies).
#
# Endpoints:
# - GET  /health
# - POST /aws/lambda-microvms/runtime/beta/v1/ready
# - POST /aws/lambda-microvms/runtime/beta/v1/launch
# - POST /aws/lambda-microvms/runtime/beta/v1/resume
# - POST /aws/lambda-microvms/runtime/beta/v1/suspend
# - POST /aws/lambda-microvms/runtime/beta/v1/terminate
# - POST /execute

require 'json'
require 'logger'
require 'stringio'
require 'webrick'

begin
  require 'datadog'
  DD_TRACER = Datadog::Tracing
rescue LoadError
  DD_TRACER = nil
end

BASE_PATH = '/aws/lambda-microvms/runtime/beta/v1'
PORT = 8080

$micro_vm_id = nil

LOGGER = Logger.new($stdout)
LOGGER.level = Logger::INFO
LOGGER.formatter = proc { |sev, ts, _, msg| "#{ts.utc.iso8601} - #{sev} - [sample-ruby-app] #{msg}\n" }

# Intentional REPL sandbox: /execute is this app's core feature. It accepts
# arbitrary Ruby code — the same role exec(code, {}) plays in the Python version.
module Sandbox
  def self.run(code)
    eval(code) # rubocop:disable Security/Eval
  end
end

def now_ts
  Time.now.utc.iso8601
end

def with_span(method, path)
  if DD_TRACER
    DD_TRACER.trace('http.request', resource: "#{method} #{path}", type: 'web') { yield }
  else
    yield
  end
end

def send_json(res, status, body)
  res.status = status
  res['Content-Type'] = 'application/json'
  res.body = JSON.generate(body)
end

def send_empty(res, status = 200)
  res.status = status
  res.body = ''
end

def handle_execute(req, res)
  data = req.body ? JSON.parse(req.body) : {}
  code = data['code'].to_s.strip

  if code.empty?
    send_json(res, 400, { 'error' => 'No code provided' })
    return
  end

  LOGGER.info("Execute called [ts=#{now_ts}, microVmId=#{$micro_vm_id}]")

  old_stdout = $stdout
  old_stderr = $stderr
  captured_stdout = StringIO.new
  captured_stderr = StringIO.new
  $stdout = captured_stdout
  $stderr = captured_stderr

  error = nil
  begin
    Sandbox.run(code)
  rescue StandardError, SyntaxError => e
    error = "#{e.class}: #{e.message}\n#{e.backtrace&.join("\n")}"
  ensure
    $stdout = old_stdout
    $stderr = old_stderr
  end

  if error
    send_json(res, 200, { 'success' => false, 'error' => error, 'stderr' => captured_stderr.string })
  else
    send_json(res, 200, { 'success' => true, 'output' => captured_stdout.string, 'stderr' => captured_stderr.string })
  end
rescue StandardError => e
  send_json(res, 500, { 'error' => e.message })
end

server = WEBrick::HTTPServer.new(
  Port: PORT,
  Logger: WEBrick::Log.new('/dev/null'),
  AccessLog: []
)

server.mount_proc('/health') do |req, res|
  with_span('GET', '/health') do
    if req.request_method == 'GET'
      LOGGER.info("Health check called [ts=#{now_ts}, microVmId=#{$micro_vm_id}]")
      send_json(res, 200, { 'status' => 'healthy' })
    else
      send_empty(res, 404)
    end
  end
end

server.mount_proc("#{BASE_PATH}/validate") do |req, res|
  with_span('POST', "#{BASE_PATH}/validate") do
    if req.request_method == 'POST'
      LOGGER.info("Validate hook called [ts=#{now_ts}, microVmId=#{$micro_vm_id}]")
      send_empty(res)
    else
      send_empty(res, 404)
    end
  end
end

server.mount_proc("#{BASE_PATH}/ready") do |req, res|
  with_span('POST', "#{BASE_PATH}/ready") do
    if req.request_method == 'POST'
      LOGGER.info("Ready hook called [ts=#{now_ts}, microVmId=#{$micro_vm_id}]")
      send_empty(res)
    else
      send_empty(res, 404)
    end
  end
end

server.mount_proc("#{BASE_PATH}/launch") do |req, res|
  with_span('POST', "#{BASE_PATH}/launch") do
    if req.request_method == 'POST'
      data = req.body ? JSON.parse(req.body) : {}
      $micro_vm_id = data['microVmId']
      mesh = data['meshIpv6Address']
      LOGGER.info("Launch hook called — ts=#{now_ts}, microVmId=#{$micro_vm_id}, meshIpv6Address=#{mesh}")
      send_empty(res)
    else
      send_empty(res, 404)
    end
  end
end

server.mount_proc("#{BASE_PATH}/resume") do |req, res|
  with_span('POST', "#{BASE_PATH}/resume") do
    if req.request_method == 'POST'
      LOGGER.info("Resume hook called [ts=#{now_ts}, microVmId=#{$micro_vm_id}]")
      send_empty(res)
    else
      send_empty(res, 404)
    end
  end
end

server.mount_proc("#{BASE_PATH}/suspend") do |req, res|
  with_span('POST', "#{BASE_PATH}/suspend") do
    if req.request_method == 'POST'
      LOGGER.info("Suspend hook called [ts=#{now_ts}, microVmId=#{$micro_vm_id}]")
      send_empty(res)
    else
      send_empty(res, 404)
    end
  end
end

server.mount_proc("#{BASE_PATH}/terminate") do |req, res|
  with_span('POST', "#{BASE_PATH}/terminate") do
    if req.request_method == 'POST'
      LOGGER.info("Terminate hook called [ts=#{now_ts}, microVmId=#{$micro_vm_id}]")
      send_empty(res)
    else
      send_empty(res, 404)
    end
  end
end

server.mount_proc('/execute') do |req, res|
  with_span('POST', '/execute') do
    if req.request_method == 'POST'
      handle_execute(req, res)
    else
      send_empty(res, 404)
    end
  end
end

LOGGER.info("Starting sample-ruby-app on port #{PORT}")
env_lines = ENV.reject { |k, _| k == 'DD_API_KEY' }.sort.map { |k, v| "  #{k}=#{v}" }.join("\n")
LOGGER.info("Environment variables:\n#{env_lines}")

puts <<~HELP

  Sample commands (server running on port #{PORT}):

    curl http://127.0.0.1:#{PORT}/health

    curl -X POST http://127.0.0.1:#{PORT}#{BASE_PATH}/ready

    curl -X POST http://127.0.0.1:#{PORT}#{BASE_PATH}/launch \\
      -H 'Content-Type: application/json' \\
      -d '{"microVmId": "hello_world", "meshIpv6Address": "::1"}'

    curl -X POST http://127.0.0.1:#{PORT}#{BASE_PATH}/resume

    curl -X POST http://127.0.0.1:#{PORT}#{BASE_PATH}/suspend

    curl -X POST http://127.0.0.1:#{PORT}#{BASE_PATH}/terminate

    curl -X POST http://127.0.0.1:#{PORT}/execute \\
      -H 'Content-Type: application/json' \\
      -d '{"code": "puts 1 + 1"}'

HELP

trap('INT')  { server.shutdown }
trap('TERM') { server.shutdown }

server.start
