targetScope = 'subscription'

@description('Azure region code used by all OPTIMA resources.')
param location string = 'eastus'

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

var resourceGroupName = 'rg-optima-${environmentName}'
var tags = {
  application: 'optima'
  environment: environmentName
  managedBy: 'bicep'
  workload: 'hackathon'
}

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module resources 'resource-group.bicep' = {
  name: 'optima-resources'
  scope: resourceGroup
  params: {
    applicationInsightsSamplingRatio: applicationInsightsSamplingRatio
    deployContainerApps: deployContainerApps
    environmentName: environmentName
    foundryBaseUrl: foundryBaseUrl
    foundrySmallDeployment: foundrySmallDeployment
    foundryStrongDeployment: foundryStrongDeployment
    foundryTokenScope: foundryTokenScope
    imageTag: imageTag
    location: location
    redisEmbeddingDeployment: redisEmbeddingDeployment
    redisEmbeddingDimension: redisEmbeddingDimension
    redisEmbeddingModel: redisEmbeddingModel
  }
}

output resourceGroupName string = resourceGroup.name
output resourceGroupId string = resourceGroup.id
output registryName string = resources.outputs.registryName
output registryLoginServer string = resources.outputs.registryLoginServer
output apiContainerAppName string = resources.outputs.apiContainerAppName
output apiUrl string = resources.outputs.apiUrl
output uiContainerAppName string = resources.outputs.uiContainerAppName
output uiUrl string = resources.outputs.uiUrl
output cosmosAccountName string = resources.outputs.cosmosAccountName
output cosmosEndpoint string = resources.outputs.cosmosEndpoint
output cosmosDatabaseName string = resources.outputs.cosmosDatabaseName
output cosmosContainerName string = resources.outputs.cosmosContainerName
output redisName string = resources.outputs.redisName
output redisHost string = resources.outputs.redisHost
output redisPort int = resources.outputs.redisPort
output redisIndexName string = resources.outputs.redisIndexName
output applicationInsightsName string = resources.outputs.applicationInsightsName
output apiIdentityResourceId string = resources.outputs.apiIdentityResourceId
output apiIdentityPrincipalId string = resources.outputs.apiIdentityPrincipalId
output uiIdentityResourceId string = resources.outputs.uiIdentityResourceId
output uiIdentityPrincipalId string = resources.outputs.uiIdentityPrincipalId
