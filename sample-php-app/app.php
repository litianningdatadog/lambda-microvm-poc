<?php
/**
 * Sample guest application implementing Lambda MicroVM lifecycle hooks.
 *
 * Runs under PHP's built-in CLI server (no external dependencies).
 *
 * Endpoints:
 *   GET  /health
 *   POST /aws/lambda-microvms/runtime/beta/v1/ready
 *   POST /aws/lambda-microvms/runtime/beta/v1/launch
 *   POST /aws/lambda-microvms/runtime/beta/v1/resume
 *   POST /aws/lambda-microvms/runtime/beta/v1/suspend
 *   POST /aws/lambda-microvms/runtime/beta/v1/terminate
 *   POST /execute
 */

const BASE_PATH  = '/aws/lambda-microvms/runtime/beta/v1';
const STATE_FILE = '/tmp/microvm_state.json';

function nowTs(): string
{
    return gmdate("Y-m-d\TH:i:s\Z");
}

function logInfo(string $msg): void
{
    fwrite(STDERR, nowTs() . " - INFO - [sample-php-app] {$msg}\n");
}

function readJsonBody(): array
{
    $raw = file_get_contents('php://input');
    if ($raw === '' || $raw === false) return [];
    return json_decode($raw, true) ?? [];
}

function sendJson(int $status, array $body): void
{
    http_response_code($status);
    header('Content-Type: application/json');
    echo json_encode($body);
}

function sendEmpty(int $status = 200): void
{
    http_response_code($status);
    header('Content-Length: 0');
}

function getMicroVmId(): ?string
{
    if (!file_exists(STATE_FILE)) return null;
    $state = json_decode(file_get_contents(STATE_FILE), true);
    return $state['microVmId'] ?? null;
}

function setMicroVmId(?string $id): void
{
    file_put_contents(STATE_FILE, json_encode(['microVmId' => $id]));
}

$method    = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$path      = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH);
$microVmId = getMicroVmId();

switch (true) {
    case $method === 'GET' && $path === '/health':
        logInfo("Health check called [ts=" . nowTs() . ", microVmId={$microVmId}]");
        sendJson(200, ['status' => 'healthy']);
        break;

    case $method === 'POST' && $path === BASE_PATH . '/validate':
        logInfo("Validate hook called [ts=" . nowTs() . ", microVmId={$microVmId}]");
        sendEmpty();
        break;

    case $method === 'POST' && $path === BASE_PATH . '/ready':
        logInfo("Ready hook called [ts=" . nowTs() . ", microVmId={$microVmId}]");
        sendEmpty();
        break;

    case $method === 'POST' && $path === BASE_PATH . '/launch':
        $data      = readJsonBody();
        $microVmId = $data['microVmId'] ?? null;
        $meshAddr  = $data['meshIpv6Address'] ?? null;
        setMicroVmId($microVmId);
        logInfo("Launch hook called — ts=" . nowTs() . ", microVmId={$microVmId}, meshIpv6Address={$meshAddr}");
        sendEmpty();
        break;

    case $method === 'POST' && $path === BASE_PATH . '/resume':
        logInfo("Resume hook called [ts=" . nowTs() . ", microVmId={$microVmId}]");
        sendEmpty();
        break;

    case $method === 'POST' && $path === BASE_PATH . '/suspend':
        logInfo("Suspend hook called [ts=" . nowTs() . ", microVmId={$microVmId}]");
        sendEmpty();
        break;

    case $method === 'POST' && $path === BASE_PATH . '/terminate':
        logInfo("Terminate hook called [ts=" . nowTs() . ", microVmId={$microVmId}]");
        sendEmpty();
        break;

    case $method === 'POST' && $path === '/execute':
        $data = readJsonBody();
        $code = $data['code'] ?? '';

        if ($code === '') {
            sendJson(400, ['error' => 'No code provided']);
            break;
        }

        logInfo("Execute called [ts=" . nowTs() . ", microVmId={$microVmId}]");

        ob_start();
        $error = null;
        try {
            eval($code);
        } catch (Throwable $e) {
            $error = get_class($e) . ': ' . $e->getMessage() . "\n" . $e->getTraceAsString();
        }
        $output = ob_get_clean();

        if ($error !== null) {
            sendJson(200, ['success' => false, 'error' => $error, 'output' => $output]);
        } else {
            sendJson(200, ['success' => true, 'output' => $output]);
        }
        break;

    default:
        sendEmpty(404);
        break;
}
