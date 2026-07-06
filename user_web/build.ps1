$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $root
try {
    if (Test-Path (Join-Path $root "package-lock.json")) {
        npm ci
    } else {
        npm install
    }
    npm run typecheck
    npm test
    npm run build
} finally {
    Pop-Location
}
