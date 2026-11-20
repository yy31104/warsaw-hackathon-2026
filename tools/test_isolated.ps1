param([string[]] $TestPath = @())

$ErrorActionPreference = 'Stop'
$testRepo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$testTag = 'dev2-suite-' + [guid]::NewGuid().ToString('N').Substring(0,10)
$testNetwork = $null
$testDb = $null
$testBroker = $null
$testCode = 1
try {
    $testNetwork = docker network create --label purpose=alertfunnel-merge-check $testTag
    if ($LASTEXITCODE -ne 0) { throw 'Could not create test network' }
    $upgradeMounts = foreach ($part in 'db','tools') {
        '--mount'
        "type=bind,source=$testRepo/$part,target=/verify/$part,readonly"
    }
    $testDb = docker run -d --name "$testTag-db" --network $testTag --network-alias db --tmpfs /var/lib/postgresql/data @upgradeMounts -e POSTGRES_USER=alerting -e POSTGRES_PASSWORD=alerting -e POSTGRES_DB=alerting postgres:16-alpine
    if ($LASTEXITCODE -ne 0) { throw 'Could not start test database' }
    $testBroker = docker run -d --name "$testTag-mq" --hostname test-broker --network $testTag --network-alias broker -e RABBITMQ_DEFAULT_USER=alerting -e RABBITMQ_DEFAULT_PASS=alerting -e 'RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS=+S 2:2' rabbitmq:3.13-management-alpine
    if ($LASTEXITCODE -ne 0) { throw 'Could not start test broker' }
    $testReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $brokerState = docker inspect --format '{{.State.Status}}' $testBroker
        if ($brokerState -eq 'exited') { break }
        docker exec $testDb pg_isready -q -U alerting -d alerting 2>$null
        $dbReady = $LASTEXITCODE -eq 0
        $brokerReady = [bool](docker logs --tail 100 $testBroker 2>&1 | Select-String -Pattern 'started TCP listener on .+:5672|Server startup complete')
        if ($dbReady -and $brokerReady) { $testReady = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $testReady) { docker logs --tail 40 $testBroker; docker logs --tail 6 $testDb; throw 'Test services unavailable' }
    docker exec $testDb createdb -U alerting upgrade_test
    if ($LASTEXITCODE) { throw 'Could not create the disposable upgrade database' }
    $baseline = Get-ChildItem (Join-Path $testRepo 'db/migrations/*.sql') |
        Where-Object { $_.Name -le '136_canary.sql' -or $_.Name -eq '201_schema_review.sql' } |
        Sort-Object Name | ForEach-Object { '\i /verify/db/migrations/' + $_.Name }
    @($baseline; '\i /verify/tools/test_db_upgrade.sql') |
        docker exec -i $testDb psql -X -q -v ON_ERROR_STOP=1 -U alerting -d upgrade_test
    if ($LASTEXITCODE) { throw 'Existing-volume database upgrade regression failed' }
    Write-Output 'Existing-volume upgrade and repeat passed. Running backend tests with PostgreSQL and RabbitMQ.'
    $repoMounts = foreach ($part in @('config', 'db', 'fixtures', 'mocks', 'loadtest', 'tools', 'docs')) {
        '--mount'
        "type=bind,source=$testRepo/$part,target=/work/$part,readonly"
    }
    $testScript = 'set -eu; mkdir -p /work/backend; cp -R /input/backend/alerting /input/backend/tests /input/backend/pyproject.toml /input/backend/uv.lock /work/backend/; cd /work/backend; python -m pip --disable-pip-version-check install -q uv; uv sync --frozen --quiet; uv run --no-sync pytest -q -ra "$@"'
    docker run --rm --name "$testTag-python" --network $testTag --mount "type=bind,source=$testRepo/backend,target=/input/backend,readonly" @repoMounts -e CANARY_LIVE_GATEWAY_ROOT=/work -e DATABASE_URL=postgresql://alerting:alerting@db:5432/alerting -e INGEST_TEST_RABBITMQ_URL=amqp://alerting:alerting@broker:5672/ -e ASSIST_ENABLED=0 -e DEMO_MODE=1 -e UV_NO_PROGRESS=1 -e PIP_ROOT_USER_ACTION=ignore python:3.12-slim sh -c $testScript run-tests @TestPath
    $testCode = $LASTEXITCODE
}
finally {
    foreach ($testService in $testBroker,$testDb) {
        if ($testService -match '^[a-f0-9]{64}$') {
            docker stop -t 3 $testService | Out-Null
            docker rm -v $testService | Out-Null
        }
    }
    if ($testNetwork -match '^[a-f0-9]{64}$') { docker network rm $testNetwork | Out-Null }
}
exit $testCode
