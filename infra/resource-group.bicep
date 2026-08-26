targetScope = 'resourceGroup'

@description('Azure region code used by all OPTIMA resources.')
param location string = resourceGroup().location

@description('Deployment environment name.')
@allowed([
  'hackathon'
])
param environmentName string = 'hackathon'

@description('Deploy API and UI only after images, runtime composition, and data-plane access are ready.')
param deployContainerApps bool = false

@description('Immutable container image tag produced by a later build slice.')
param imageTag string

@description('Foundry or APIM Azure OpenAI v1 API root.')
param foundryBaseUrl string

@description('Foundry deployment mapped to the OPTIMA SMALL role.')
param foundrySmallDeployment string

@description('Foundry deployment mapped to the OPTIMA STRONG role.')
param foundryStrongDeployment string

@description('OAuth token scope accepted by the configured Foundry or APIM endpoint.')
param foundryTokenScope string = 'https://cognitiveservices.azure.com/.default'

@description('Foundry embedding deployment used by the semantic cache.')
param redisEmbeddingDeployment string

@description('Provider-reported embedding model identity.')
param redisEmbeddingModel string

@description('Embedding vector dimension shared by Foundry and RediSearch.')
@minValue(1)
@maxValue(32768)
param redisEmbeddingDimension int

@description('Root trace sampling ratio for Application Insights.')
@allowed([
  '0.1'
  '0.25'
  '0.5'
  '1.0'
])
param applicationInsightsSamplingRatio string = '0.25'

var uniqueSuffix = uniqueString(subscription().subscriptionId, environmentName)
var resourceNames = {
  apiContainerApp: 'ca-optima-api-${environmentName}'
  apiIdentity: 'id-optima-api-${environmentName}'
  applicationInsights: 'appi-optima-${environmentName}'
  containerAppsEnvironment: 'cae-optima-${environmentName}'
  containerRegistry: 'acroptima${uniqueSuffix}'
  cosmosAccount: 'cosmos-optima-${uniqueSuffix}'
  logAnalyticsWorkspace: 'law-optima-${environmentName}'
  redis: 'redis-optima-${uniqueSuffix}'
  uiContainerApp: 'ca-optima-ui-${environmentName}'
  uiIdentity: 'id-optima-ui-${environmentName}'
}
var tags = {
  application: 'optima'
  environment: environmentName
  managedBy: 'bicep'
  workload: 'hackathon'
}

module identities 'modules/identities.bicep' = {
  name: 'optima-identities'
  params: {
    apiIdentityName: resourceNames.apiIdentity
    location: location
    tags: tags
    uiIdentityName: resourceNames.uiIdentity
  }
}

module registry 'modules/container-registry.bicep' = {
  name: 'optima-container-registry'
  params: {
    location: location
    registryName: resourceNames.containerRegistry
    tags: tags
  }
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'optima-monitoring'
  params: {
    applicationInsightsName: resourceNames.applicationInsights
    location: location
    logAnalyticsWorkspaceName: resourceNames.logAnalyticsWorkspace
    tags: tags
  }
}

module cosmos 'modules/cosmos.bicep' = {
  name: 'optima-cosmos'
  params: {
    accountName: resourceNames.cosmosAccount
    containerName: 'runs'
    databaseName: 'optima'
    location: location
    tags: tags
  }
}

module redis 'modules/managed-redis.bicep' = {
  name: 'optima-managed-redis'
  params: {
    location: location
    redisName: resourceNames.redis
    tags: tags
  }
}

module containerApps 'modules/container-apps.bicep' = if (deployContainerApps) {
  name: 'optima-container-apps'
  params: {
    apiContainerAppName: resourceNames.apiContainerApp
    apiIdentityClientId: identities.outputs.apiClientId
    apiIdentityPrincipalId: identities.outputs.apiPrincipalId
    apiIdentityResourceId: identities.outputs.apiResourceId
    apiImage: '${registry.outputs.loginServer}/optima-api:${imageTag}'
    applicationInsightsConnectionString: monitoring.outputs.connectionString
    applicationInsightsSamplingRatio: applicationInsightsSamplingRatio
    containerAppsEnvironmentName: resourceNames.containerAppsEnvironment
    cosmosContainerName: cosmos.outputs.containerName
    cosmosDatabaseName: cosmos.outputs.databaseName
    cosmosEndpoint: cosmos.outputs.endpoint
    environmentName: environmentName
    foundryBaseUrl: foundryBaseUrl
    foundrySmallDeployment: foundrySmallDeployment
    foundryStrongDeployment: foundryStrongDeployment
    foundryTokenScope: foundryTokenScope
    location: location
    redisEmbeddingDeployment: redisEmbeddingDeployment
    redisEmbeddingDimension: redisEmbeddingDimension
    redisEmbeddingModel: redisEmbeddingModel
    redisHost: redis.outputs.hostName
    redisIndexName: 'optima-cache-v1'
    registryLoginServer: registry.outputs.loginServer
    tags: tags
    uiContainerAppName: resourceNames.uiContainerApp
    uiIdentityResourceId: identities.outputs.uiResourceId
    uiImage: '${registry.outputs.loginServer}/optima-ui:${imageTag}'
  }
}

output registryName string = resourceNames.containerRegistry
output registryLoginServer string = registry.outputs.loginServer
output apiContainerAppName string = resourceNames.apiContainerApp
output apiUrl string = deployContainerApps ? containerApps!.outputs.apiUrl : ''
output uiContainerAppName string = resourceNames.uiContainerApp
output uiUrl string = deployContainerApps ? containerApps!.outputs.uiUrl : ''
output cosmosAccountName string = resourceNames.cosmosAccount
output cosmosEndpoint string = cosmos.outputs.endpoint
output cosmosDatabaseName string = cosmos.outputs.databaseName
output cosmosContainerName string = cosmos.outputs.containerName
output redisName string = resourceNames.redis
output redisHost string = redis.outputs.hostName
output redisPort int = redis.outputs.port
output redisIndexName string = 'optima-cache-v1'
output applicationInsightsName string = resourceNames.applicationInsights
output apiIdentityResourceId string = identities.outputs.apiResourceId
output apiIdentityPrincipalId string = identities.outputs.apiPrincipalId
output uiIdentityResourceId string = identities.outputs.uiResourceId
output uiIdentityPrincipalId string = identities.outputs.uiPrincipalId
