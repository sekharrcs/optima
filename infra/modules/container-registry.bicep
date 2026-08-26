@description('Azure region code for the registry.')
param location string

@description('Globally unique Azure Container Registry name.')
param registryName string

@description('Common resource tags.')
param tags object

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    dataEndpointEnabled: false
    networkRuleBypassOptions: 'AzureServices'
    publicNetworkAccess: 'Enabled'
    zoneRedundancy: 'Disabled'
  }
}

output resourceId string = registry.id
output loginServer string = registry.properties.loginServer
