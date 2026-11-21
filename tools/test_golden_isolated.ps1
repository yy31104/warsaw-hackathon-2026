# Run the repo's destructive demo resets only against a newly named Compose stack.
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$tag = 'dev2gold' + [Guid]::NewGuid().ToString('N').Substring(0, 9)
$logDir = Join-Path $repoRoot "loadtest/results/tmp/$tag"
$composeArgs = @('compose', '-p', $tag, '-f', (Join-Path $repoRoot 'docker-compose.yml'))
$ports = 'POSTGRES_PORT','RABBITMQ_PORT','RABBITMQ_MANAGEMENT_PORT','SMTP_PORT','MAILPIT_PORT','TELECOM_PORT','MOCK_OPS_PORT','API_PORT','FRONTEND_PORT'
$original = @{}
$code = 1
try {
    [IO.Directory]::CreateDirectory($logDir) | Out-Null
    [IO.File]::WriteAllText((Join-Path $logDir 'pricing-engine.log'), '')
    foreach ($key in $ports + @('LOG_DIR','ASSIST_ENABLED','DEMO_MODE','ANTHROPIC_API_KEY','INBOUND_SECRET')) {
        $original[$key] = [Environment]::GetEnvironmentVariable($key)
    }
    foreach ($key in $ports) { [Environment]::SetEnvironmentVariable($key, '0') }
    $env:DEMO_MODE = '1'
    $env:ASSIST_ENABLED = '0'
    $env:ANTHROPIC_API_KEY = ''
    $env:INBOUND_SECRET = 'alertfunnel-local-demo'
    $env:LOG_DIR = $logDir
    docker @composeArgs up --build --wait --wait-timeout 180 -d api fake-telecom mock-ops ingest-amqp ingest-logtail rule-worker rule-worker-critical scheduler delivery-worker
    if ($LASTEXITCODE) { throw "Could not start $tag" }
    $mounts = foreach ($part in 'config','fixtures','loadtest') {
        '--mount'
        "type=bind,source=$repoRoot/$part,target=/verify/$part,readonly"
    }
    docker run --rm --network "${tag}_default" @mounts --mount "type=bind,source=$logDir,target=/logs" -e RABBITMQ_URL=amqp://guest:guest@rabbitmq:5672/ -e INBOUND_SECRET=alertfunnel-local-demo "${tag}-backend:local" python /verify/loadtest/golden_runner.py --broker --no-write --log-input /logs/pricing-engine.log --api-url http://api:8000 --telecom-url http://fake-telecom:8100 --mailpit-url http://mailpit:8025
    $code = $LASTEXITCODE
} finally {
    docker @composeArgs down --volumes --remove-orphans | Out-Host
    foreach ($key in $original.Keys) { [Environment]::SetEnvironmentVariable($key, $original[$key]) }
}
exit $code
