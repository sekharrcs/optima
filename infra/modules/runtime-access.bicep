@description('Azure Container Registry name.')
param registryName string

@description('API managed identity principal ID.')
param apiPrincipalId string

@description('UI managed identity principal ID.')
param uiPrincipalId string

@description('Cosmos DB account name.')
param cosmosAccountName string

@description('Cosmos DB for NoSQL database name.')
param cosmosDatabaseName string

@description('Cosmos DB for NoSQL container name.')
param cosmosContainerName string

@description('Azure Managed Redis name.')
param redisName string

var acrPullRoleDefinitionId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '7f951dda-4ed3-4680-a7ca-43fe172d538d'
)
var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource apiRegistryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, apiPrincipalId, acrPullRoleDefinitionId)
  scope: registry
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleDefinitionId
  }
}

resource uiRegistryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, uiPrincipalId, acrPullRoleDefinitionId)
  scope: registry
  properties: {
    principalId: uiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleDefinitionId
  }
}

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' existing = {
  name: cosmosAccountName
}

resource cosmosDataContributorRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleDefinitions@2024-11-15' existing = {
  name: cosmosDataContributorRoleId
  parent: cosmosAccount
}

var cosmosContainerScope = '${cosmosAccount.id}/dbs/${cosmosDatabaseName}/colls/${cosmosContainerName}'

resource apiCosmosDataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  name: guid(cosmosAccount.id, apiPrincipalId, cosmosDataContributorRole.id, cosmosContainerScope)
  parent: cosmosAccount
  properties: {
    principalId: apiPrincipalId
    roleDefinitionId: cosmosDataContributorRole.id
    scope: cosmosContainerScope
  }
}

resource redis 'Microsoft.Cache/redisEnterprise@2025-07-01' existing = {
  name: redisName
}

resource redisDatabase 'Microsoft.Cache/redisEnterprise/databases@2025-07-01' existing = {
  name: 'default'
  parent: redis
}

resource apiRedisAccess 'Microsoft.Cache/redisEnterprise/databases/accessPolicyAssignments@2025-07-01' = {
  name: replace(guid(redisDatabase.id, apiPrincipalId), '-', '')
  parent: redisDatabase
  properties: {
    accessPolicyName: 'default'
    user: {
      objectId: apiPrincipalId
    }
  }
}