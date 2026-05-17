[CmdletBinding()]
param(
  [string] $Location  = 'eastus',
  [string] $RgName    = 'insecure-llm-rg',
  [string] $BaseName  = 'illm',
  [string] $BasicAuthUsername = 'demo',
  [string] $BasicAuthPassword,
  [int]    $MaxUploadBytes = 10485760,
  [string] $ImageTag  = 'v1'
)

# Use Continue, not Stop: native `az` calls write warnings (e.g. "new Bicep
# release available") to stderr and PS 5.1 with Stop turns those into
# terminating errors before our explicit `$LASTEXITCODE` check runs.
$ErrorActionPreference = 'Continue'

if (-not $BasicAuthPassword) {
  $bytes = New-Object byte[] 24
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $BasicAuthPassword = [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_').TrimEnd('=')
  Write-Host "Generated BASIC_AUTH_PASSWORD: $BasicAuthPassword"
}

$repoRoot   = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$mainBicep  = Join-Path $PSScriptRoot 'main.bicep'
$deployName = "insecure-llm-init-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))"

Write-Host "==> Step 1/3: deploying infra (Bicep)..."
$deployJson = az deployment sub create `
  --name $deployName `
  --location $Location `
  --template-file $mainBicep `
  --parameters `
    location=$Location `
    rgName=$RgName `
    baseName=$BaseName `
    basicAuthUsername=$BasicAuthUsername `
    basicAuthPassword=$BasicAuthPassword `
    maxUploadBytes=$MaxUploadBytes `
  --output json
if ($LASTEXITCODE -ne 0) { throw "Bicep deployment failed." }

$deployment = $deployJson | ConvertFrom-Json
$acrName = $deployment.properties.outputs.containerRegistryName.value
$caName  = $deployment.properties.outputs.containerAppName.value
$fqdn    = $deployment.properties.outputs.containerAppFqdn.value
$aisName = $deployment.properties.outputs.aiServicesName.value
$hubName = $deployment.properties.outputs.foundryHubName.value

# Foundry auto-creates an `<hub>_aoai` connection at hub-creation time and
# pins its DeploymentApiVersion to 2023-10-01-preview, which can't see
# gpt-5.x deployments. Bumping it here so the agent runtime can resolve
# the newer model deployments. Idempotent.
Write-Host "==> Step 1.5/3: bumping Foundry AOAI connection metadata to a current API version..."
$subId = az account show --query id -o tsv
$aisKey = az cognitiveservices account keys list --name $aisName --resource-group $RgName --query key1 -o tsv
$resourceId = "/subscriptions/$subId/resourceGroups/$RgName/providers/Microsoft.CognitiveServices/accounts/$aisName"
$connBody = @{
  properties = @{
    category = 'AzureOpenAI'
    target = "https://$aisName.openai.azure.com/"
    authType = 'ApiKey'
    isSharedToAll = $true
    credentials = @{ key = $aisKey }
    metadata = @{
      ApiType = 'Azure'
      ApiVersion = '2025-04-01-preview'
      DeploymentApiVersion = '2025-04-01-preview'
      ResourceId = $resourceId
    }
  }
} | ConvertTo-Json -Depth 10 -Compress
$connBodyPath = Join-Path ([System.IO.Path]::GetTempPath()) "illm-conn-body.json"
Set-Content -Path $connBodyPath -Value $connBody -Encoding utf8 -NoNewline
# Foundry suffixes "_aoai" onto the AI Services connection I create in
# Bicep (named "aiservices"). The resulting auto-created connection is
# what the agent runtime uses for model routing.
$connUrl = "https://management.azure.com/subscriptions/$subId/resourceGroups/$RgName/providers/Microsoft.MachineLearningServices/workspaces/$hubName/connections/aiservices_aoai?api-version=2024-10-01"
az rest --method put --url $connUrl --body "@$connBodyPath" --headers "Content-Type=application/json" --output none
if ($LASTEXITCODE -ne 0) {
  Write-Host "WARNING: connection bump returned non-zero ($LASTEXITCODE). gpt-5.x routing may not work until this is fixed manually." -ForegroundColor Yellow
}
Remove-Item $connBodyPath -ErrorAction SilentlyContinue

Write-Host "==> Step 2/3: building image in ACR ($acrName)..."
# --no-logs avoids az's local cp1252-vs-UTF-8 log-stream encoding crash on Windows.
# The actual build status is queried via ACR runs after the call returns.
az acr build --registry $acrName --image "insecure-llm-app:$ImageTag" --no-logs $repoRoot
if ($LASTEXITCODE -ne 0) { throw "ACR build failed." }

$image = "$acrName.azurecr.io/insecure-llm-app:$ImageTag"
Write-Host "==> Step 3/3: pointing container app at $image ..."
az containerapp update --name $caName --resource-group $RgName --image $image | Out-Null
if ($LASTEXITCODE -ne 0) { throw "containerapp update failed." }

Write-Host ""
Write-Host "Deployed."
Write-Host "  URL:      https://$fqdn"
Write-Host "  Username: $BasicAuthUsername"
Write-Host "  Password: $BasicAuthPassword"
Write-Host ""
Write-Host "Tear down with:  ./infra/teardown.ps1"
