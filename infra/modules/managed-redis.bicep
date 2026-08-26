@description('Azure region code for Azure Managed Redis.')
param location string

@description('Region-unique Azure Managed Redis name.')
param redisName string

@description('Common resource tags.')
param tags object

resource redis 'Microsoft.Cache/redisEnterprise@2025-07-01' = {
  name: redisName
  location: location
  tags: tags
  sku: {
    name: 'Balanced_B0'
  }
  properties: {
    encryption: {}
    highAvailability: 'Disabled'
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
  }
}

resource database 'Microsoft.Cache/redisEnterprise/databases@2025-07-01' = {
  name: 'default'
  parent: redis
  properties: {
    accessKeysAuthentication: 'Disabled'
    clientProtocol: 'Encrypted'
    clusteringPolicy: 'EnterpriseCluster'
    evictionPolicy: 'NoEviction'
    modules: [
      {
        name: 'RediSearch'
      }
    ]
    persistence: {
      aofEnabled: false
      rdbEnabled: false
    }
    port: 10000
  }
}

output resourceId string = redis.id
output databaseResourceId string = database.id
output hostName string = '${redis.name}.${location}.redis.azure.net'
output port int = database.properties.port
