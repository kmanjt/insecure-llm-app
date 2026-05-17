[CmdletBinding()]
param(
  [string] $RgName = 'insecure-llm-rg',
  [switch] $Wait
)

$ErrorActionPreference = 'Stop'

Write-Host "Deleting resource group '$RgName'..."

$azArgs = @('group','delete','--name',$RgName,'--yes')
if (-not $Wait) { $azArgs += '--no-wait' }

az @azArgs
if ($LASTEXITCODE -ne 0) { throw "az group delete failed." }

# Purge soft-deleted Cognitive Services accounts (AI Services / OpenAI) so
# their custom subdomains free up immediately for the next deployment.
Write-Host "Purging any soft-deleted Cognitive Services accounts in this RG..."
$deletedJson = az cognitiveservices account list-deleted --output json 2>$null
if ($deletedJson) {
  $deleted = $deletedJson | ConvertFrom-Json
  foreach ($acct in $deleted) {
    if ($acct.properties.resourceGroup -eq $RgName) {
      Write-Host "  purging $($acct.name) in $($acct.location)"
      az cognitiveservices account purge `
        --location $acct.location `
        --resource-group $RgName `
        --name $acct.name | Out-Null
    }
  }
}

# Purge soft-deleted Key Vaults (RBAC + 7-day soft-delete is enabled on the
# Hub-backing vault) so the name frees up immediately.
Write-Host "Purging any soft-deleted Key Vaults in this RG..."
$kvDeletedJson = az keyvault list-deleted --output json 2>$null
if ($kvDeletedJson) {
  $kvDeleted = $kvDeletedJson | ConvertFrom-Json
  foreach ($kv in $kvDeleted) {
    if ($kv.properties.deletionDate -and ($kv.name -like '*illm*' -or $kv.name -like '*kv*')) {
      Write-Host "  purging $($kv.name) in $($kv.properties.location)"
      az keyvault purge --name $kv.name --location $kv.properties.location | Out-Null
    }
  }
}

Write-Host ""
Write-Host "Teardown initiated."
Write-Host ""
Write-Host "Note: Foundry Hub + Project workspaces soft-delete for ~14 days. If you redeploy in the"
Write-Host "same RG within that window you may hit workspace-name conflicts. Either wait it out or"
Write-Host "rerun deploy.ps1 with a different -BaseName."
