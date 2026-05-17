@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Base name prefix.')
param baseName string

@description('Basic Auth username.')
param basicAuthUsername string

@secure()
param basicAuthPassword string

@description('Container image reference. Placeholder on first deploy, then overwritten by the deploy script.')
param containerImage string

@minValue(30)
@maxValue(730)
param logRetentionDays int = 30

param maxUploadBytes int = 10485760

@secure()
param sonnylabsApiKey string = ''

param sonnylabsBaseUrl string = ''

param chatModelName string = 'gpt-4o-mini'
param chatModelVersion string = '2024-07-18'
param chatModelCapacity int = 20

var suffix = toLower(substring(uniqueString(resourceGroup().id), 0, 6))

var identityName     = '${baseName}-id-${suffix}'
var logName          = '${baseName}-logs-${suffix}'
var appInsightsName  = '${baseName}-appi-${suffix}'
var keyVaultName     = '${baseName}-kv-${suffix}'
var acrName          = '${baseName}acr${suffix}'
var storageName      = '${baseName}st${suffix}'
var searchName       = '${baseName}-search-${suffix}'
var aiServicesName   = '${baseName}-ais-${suffix}'
var foundryHubName   = '${baseName}-hub-${suffix}'
var foundryProjectName = '${baseName}-proj-${suffix}'
var caeName          = '${baseName}-cae-${suffix}'
var caName           = '${baseName}-app'
var caBName          = '${baseName}-app-b'
var deployVB         = !empty(sonnylabsApiKey)

// ---------------------------------------------------------------------------
// Shared identity for the container app
// ---------------------------------------------------------------------------
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

// ---------------------------------------------------------------------------
// Observability backing for both Container Apps Env and Foundry Hub
// ---------------------------------------------------------------------------
resource logws 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: logRetentionDays
    workspaceCapping: {
      dailyQuotaGb: 1
    }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logws.id
  }
}

// ---------------------------------------------------------------------------
// Foundry Hub backing: Key Vault
// ---------------------------------------------------------------------------
resource keyvault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: null
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// Container registry
// ---------------------------------------------------------------------------
resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: true
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, identity.id, 'AcrPull')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '7f951dda-4ed3-4680-a7ca-43fe172d538d'
    )
  }
}

// ---------------------------------------------------------------------------
// Storage account: backs the Foundry Hub AND hosts the public `documents`
// container that users upload into.
// ---------------------------------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    allowSharedKeyAccess: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource documentsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'documents'
}

// Container App's UAI gets data-plane access to the documents container.
resource blobDataRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, identity.id, 'StorageBlobDataContributor')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
    )
  }
}

// ---------------------------------------------------------------------------
// Azure AI Search (wired as a Foundry Hub connection so the agent can use
// the `azure_ai_search` tool alongside `file_search`).
// ---------------------------------------------------------------------------
// Azure allows exactly one `free` Search service per subscription. The
// deploy will fail if another one already exists anywhere on this sub.
resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: searchName
  location: location
  sku: { name: 'free' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
  }
}

// ---------------------------------------------------------------------------
// Azure AI Services (multi-service: hosts the chat model deployment + the
// Foundry Agent runtime + Content Safety defaults).
// ---------------------------------------------------------------------------
resource aiservices 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: aiServicesName
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: aiServicesName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: false
  }
}

// Multiple chat-model deployments so the UI can offer a picker. Each is on
// GlobalStandard / capacity 20 — well within the per-model quota in eastus
// at the time of writing. dependsOn-chained so they deploy serially (the
// Cognitive Services account rejects concurrent deployment writes).
resource chatDeploy 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiservices
  name: chatModelName
  sku: { name: 'GlobalStandard', capacity: chatModelCapacity }
  properties: {
    model: { format: 'OpenAI', name: chatModelName, version: chatModelVersion }
  }
}

resource gpt4o 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiservices
  name: 'gpt-4o'
  sku: { name: 'GlobalStandard', capacity: 20 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-4o', version: '2024-11-20' }
  }
  dependsOn: [ chatDeploy ]
}

resource gpt41mini 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiservices
  name: 'gpt-4.1-mini'
  sku: { name: 'GlobalStandard', capacity: 20 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-4.1-mini', version: '2025-04-14' }
  }
  dependsOn: [ gpt4o ]
}

resource gpt41 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiservices
  name: 'gpt-4.1'
  sku: { name: 'GlobalStandard', capacity: 20 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-4.1', version: '2025-04-14' }
  }
  dependsOn: [ gpt41mini ]
}

resource o4mini 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiservices
  name: 'o4-mini'
  sku: { name: 'GlobalStandard', capacity: 20 }
  properties: {
    model: { format: 'OpenAI', name: 'o4-mini', version: '2025-04-16' }
  }
  dependsOn: [ gpt41 ]
}

// gpt-5.3-5.4 series — the actual models exposed in the UI picker. Older
// deployments above are left in place so existing threads don't break, but
// the SUPPORTED_MODELS list in app/foundry_client.py keeps them hidden.

resource gpt54nano 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiservices
  name: 'gpt-5.4-nano'
  sku: { name: 'GlobalStandard', capacity: 20 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-5.4-nano', version: '2026-03-17' }
  }
  dependsOn: [ o4mini ]
}

resource gpt54mini 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiservices
  name: 'gpt-5.4-mini'
  sku: { name: 'GlobalStandard', capacity: 20 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-5.4-mini', version: '2026-03-17' }
  }
  dependsOn: [ gpt54nano ]
}

resource gpt54 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiservices
  name: 'gpt-5.4'
  sku: { name: 'GlobalStandard', capacity: 20 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-5.4', version: '2026-03-05' }
  }
  dependsOn: [ gpt54mini ]
}

resource gpt53chat 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiservices
  name: 'gpt-5.3-chat'
  sku: { name: 'GlobalStandard', capacity: 20 }
  properties: {
    model: { format: 'OpenAI', name: 'gpt-5.3-chat', version: '2026-03-03' }
  }
  dependsOn: [ gpt54 ]
}

// Container App's UAI can call AI Services directly (fallback path).
resource aisRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aiservices
  name: guid(aiservices.id, identity.id, 'CognitiveServicesUser')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'a97b65f3-24c7-4388-baec-2e87135dc908'
    )
  }
}

// ---------------------------------------------------------------------------
// Foundry Hub workspace + connections + Project workspace
// ---------------------------------------------------------------------------
resource hub 'Microsoft.MachineLearningServices/workspaces@2024-10-01' = {
  name: foundryHubName
  location: location
  kind: 'Hub'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    friendlyName: 'insecure-llm-app hub'
    description: 'Foundry hub for the insecure-llm-app demo'
    storageAccount: storage.id
    keyVault: keyvault.id
    applicationInsights: appInsights.id
    publicNetworkAccess: 'Enabled'
  }
}

resource hubAisConnection 'Microsoft.MachineLearningServices/workspaces/connections@2024-10-01' = {
  parent: hub
  name: 'aiservices'
  properties: {
    category: 'AIServices'
    target: aiservices.properties.endpoint
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: aiservices.listKeys().key1
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: aiservices.id
    }
  }
}

resource hubSearchConnection 'Microsoft.MachineLearningServices/workspaces/connections@2024-10-01' = {
  parent: hub
  name: 'aisearch'
  properties: {
    category: 'CognitiveSearch'
    target: 'https://${search.name}.search.windows.net'
    authType: 'ApiKey'
    isSharedToAll: true
    credentials: {
      key: search.listAdminKeys().primaryKey
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: search.id
    }
  }
}

resource project 'Microsoft.MachineLearningServices/workspaces@2024-10-01' = {
  name: foundryProjectName
  location: location
  kind: 'Project'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    friendlyName: 'insecure-llm-app project'
    description: 'Foundry project for the insecure-llm-app demo'
    hubResourceId: hub.id
    publicNetworkAccess: 'Enabled'
  }
  dependsOn: [
    hubAisConnection
    hubSearchConnection
  ]
}

// Container App's UAI gets agent/thread/file ops on the project.
resource projectRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, identity.id, 'AzureAIDeveloper')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '64702f94-c441-49e6-a78b-ef80e0188fee'
    )
  }
}

// ---------------------------------------------------------------------------
// Container Apps Environment + App
// ---------------------------------------------------------------------------
resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: caeName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logws.properties.customerId
        sharedKey: logws.listKeys().primarySharedKey
      }
    }
  }
}

var projectConnectionString = '${location}.api.azureml.ms;${subscription().subscriptionId};${resourceGroup().name};${project.name}'

resource ca 'Microsoft.App/containerApps@2024-03-01' = {
  name: caName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: [
        { name: 'basic-auth-password', value: basicAuthPassword }
        { name: 'search-key', value: search.listAdminKeys().primaryKey }
      ]
    }
    template: {
      containers: [
        {
          name: 'app'
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
          env: [
            { name: 'BASIC_AUTH_USERNAME', value: basicAuthUsername }
            { name: 'BASIC_AUTH_PASSWORD', secretRef: 'basic-auth-password' }
            { name: 'MAX_UPLOAD_BYTES', value: string(maxUploadBytes) }
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'AZURE_STORAGE_ACCOUNT', value: storage.name }
            { name: 'AZURE_STORAGE_CONTAINER', value: 'documents' }
            { name: 'AZURE_SEARCH_ENDPOINT', value: 'https://${search.name}.search.windows.net' }
            { name: 'AZURE_SEARCH_INDEX', value: 'documents' }
            { name: 'AZURE_SEARCH_KEY', secretRef: 'search-key' }
            { name: 'AZURE_AI_PROJECT_CONNECTION_STRING', value: projectConnectionString }
            { name: 'AZURE_AI_AGENT_MODEL', value: chatModelName }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 2
      }
    }
  }
  dependsOn: [
    acrPull
    blobDataRole
    aisRole
    projectRole
    chatDeploy
    gpt4o
    gpt41mini
    gpt41
    o4mini
    gpt54nano
    gpt54mini
    gpt54
    gpt53chat
    documentsContainer
  ]
}

// ---------------------------------------------------------------------------
// Version B: a second Container App on the same env, same image, same
// backend services, with FIREWALL_ENABLED + SONNYLABS_API_KEY set so
// app/firewall.py wraps every chat round-trip with a SonnyLabs scan.
// Only deployed when sonnylabsApiKey is provided. The v B app is meant
// to be compared side-by-side with v A under prompt-injection attack.
// ---------------------------------------------------------------------------
resource caB 'Microsoft.App/containerApps@2024-03-01' = if (deployVB) {
  name: caBName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: [
        { name: 'basic-auth-password', value: basicAuthPassword }
        { name: 'search-key', value: search.listAdminKeys().primaryKey }
        { name: 'sonnylabs-api-key', value: sonnylabsApiKey }
      ]
    }
    template: {
      containers: [
        {
          name: 'app'
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
          env: [
            { name: 'BASIC_AUTH_USERNAME', value: basicAuthUsername }
            { name: 'BASIC_AUTH_PASSWORD', secretRef: 'basic-auth-password' }
            { name: 'MAX_UPLOAD_BYTES', value: string(maxUploadBytes) }
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'AZURE_STORAGE_ACCOUNT', value: storage.name }
            { name: 'AZURE_STORAGE_CONTAINER', value: 'documents' }
            { name: 'AZURE_SEARCH_ENDPOINT', value: 'https://${search.name}.search.windows.net' }
            { name: 'AZURE_SEARCH_INDEX', value: 'documents' }
            { name: 'AZURE_SEARCH_KEY', secretRef: 'search-key' }
            { name: 'AZURE_AI_PROJECT_CONNECTION_STRING', value: projectConnectionString }
            { name: 'AZURE_AI_AGENT_MODEL', value: chatModelName }
            { name: 'FIREWALL_ENABLED', value: 'true' }
            { name: 'SONNYLABS_API_KEY', secretRef: 'sonnylabs-api-key' }
            { name: 'SONNYLABS_BASE_URL', value: sonnylabsBaseUrl }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 2
      }
    }
  }
  dependsOn: [
    acrPull
    blobDataRole
    aisRole
    projectRole
    chatDeploy
    gpt4o
    gpt41mini
    gpt41
    o4mini
    gpt54nano
    gpt54mini
    gpt54
    gpt53chat
    documentsContainer
  ]
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------
output containerAppName string = ca.name
output containerAppFqdn string = ca.properties.configuration.ingress.fqdn
output containerAppBName string = deployVB ? caBName : ''
output containerAppBFqdn string = deployVB ? caB!.properties.configuration.ingress.fqdn : ''
output containerRegistryName string = acr.name
output containerRegistryLoginServer string = acr.properties.loginServer
output aiServicesName string = aiservices.name
output foundryHubName string = hub.name
output foundryProjectName string = project.name
output foundryProjectConnectionString string = projectConnectionString
output storageAccountName string = storage.name
output searchServiceName string = search.name
