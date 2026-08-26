@description('Azure region code for Cosmos DB.')
param location string

@description('Globally unique Cosmos DB account name.')
param accountName string

@description('Cosmos DB for NoSQL database name.')
param databaseName string

@description('Cosmos DB for NoSQL run-history container name.')
param containerName string

@description('Common resource tags.')
param tags object

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: accountName
  location: location
  kind: 'GlobalDocumentDB'
  tags: tags
  properties: {
    backupPolicy: {
      periodicModeProperties: {
        backupIntervalInMinutes: 240
        backupRetentionIntervalInHours: 8
        backupStorageRedundancy: 'Local'
      }
      type: 'Periodic'
    }
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    databaseAccountOfferType: 'Standard'
    disableKeyBasedMetadataWriteAccess: true
    disableLocalAuth: true
    enableAnalyticalStorage: false
    enableAutomaticFailover: false
    enableFreeTier: false
    enableMultipleWriteLocations: false
    ipRules: []
    isVirtualNetworkFilterEnabled: false
    locations: [
      {
        failoverPriority: 0
        isZoneRedundant: false
        locationName: location
      }
    ]
    minimalTlsVersion: 'Tls12'
    networkAclBypass: 'None'
    networkAclBypassResourceIds: []
    publicNetworkAccess: 'Enabled'
    virtualNetworkRules: []
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  name: databaseName
  parent: account
  properties: {
    options: {}
    resource: {
      id: databaseName
    }
  }
}

resource runHistoryContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = {
  name: containerName
  parent: database
  properties: {
    options: {}
    resource: {
      id: containerName
      indexingPolicy: {
        automatic: true
        excludedPaths: [
          {
            path: '/"_etag"/?'
          }
        ]
        includedPaths: [
          {
            path: '/*'
          }
        ]
        indexingMode: 'consistent'
      }
      partitionKey: {
        kind: 'Hash'
        paths: [
          '/id'
        ]
        version: 2
      }
    }
  }
}

output accountResourceId string = account.id
output endpoint string = account.properties.documentEndpoint
output databaseName string = database.name
output containerName string = runHistoryContainer.name
