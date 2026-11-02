#!/usr/bin/env node
'use strict'

const { mkdir, writeFile } = require('node:fs/promises')
const path = require('node:path')

async function request (url, route, method = 'GET') {
  const headers = { 'content-type': 'application/json' }
  if (process.env.MICROVM_TOKEN) {
    headers['X-aws-proxy-auth'] = process.env.MICROVM_TOKEN
    headers['X-aws-proxy-port'] = '8080'
  }
  const response = await fetch(new URL(route, url), {
    method, headers, ...(method === 'POST' ? { body: '{}' } : {}), signal: AbortSignal.timeout(90000)
  })
  const value = await response.json()
  if (!response.ok) throw new Error(`${method} ${route}: HTTP ${response.status}: ${JSON.stringify(value)}`)
  return value
}

async function main () {
  let url = 'http://127.0.0.1:8080'
  let output = 'results/latest.json'
  let simulate = false
  const args = process.argv.slice(2)
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--url' && args[i + 1]) url = args[++i]
    else if (args[i] === '--output' && args[i + 1]) output = args[++i]
    else if (args[i] === '--simulate') simulate = true
    else if (args[i] === '--help') {
      console.log('Usage: node verify.js [--url URL] [--output FILE] [--simulate]\n--simulate: loopback-only ready/run simulation. Never use on AWS.\nMICROVM_TOKEN supplies ingress auth without placing it on the command line.')
      return
    } else throw new Error(`Unknown or incomplete argument: ${args[i]}`)
  }
  if (simulate) {
    const target = new URL(url)
    if (target.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(target.hostname)) {
      throw new Error('--simulate is restricted to local HTTP loopback targets')
    }
    await request(url, '/aws/lambda-microvms/runtime/v1/ready', 'POST')
    await request(url, '/aws/lambda-microvms/runtime/v1/run', 'POST')
  }
  const result = await request(url, '/verify', 'POST')
  result.execution = simulate ? 'local-lifecycle-simulation-not-a-snapshot' : 'existing-platform-run'
  await mkdir(path.dirname(output), { recursive: true })
  await writeFile(output, JSON.stringify(result, null, 2) + '\n')
  for (const [name, passed] of Object.entries(result.checks)) console.log(`${passed ? 'PASS' : 'FAIL'} ${name}`)
  console.log(`Evidence: ${output}`)
  if (!result.passed) process.exitCode = 1
}

if (require.main === module) main().catch(error => { console.error(error.message); process.exitCode = 1 })
module.exports = { request }
