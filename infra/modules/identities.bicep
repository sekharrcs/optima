@description('Azure region code for the managed identities.')
param location string

@description('API user-assigned managed identity name.')
param apiIdentityName string

@description('UI user-assigned managed identity name.')
param uiIdentityName string

@description('Common resource tags.')
param tags object

resource apiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: apiIdentityName
  location: location
  tags: tags
}

resource uiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: uiIdentityName
  location: location
  tags: tags
}

output apiResourceId string = apiIdentity.id
output apiClientId string = apiIdentity.properties.clientId
output apiPrincipalId string = apiIdentity.properties.principalId
output uiResourceId string = uiIdentity.id
output uiClientId string = uiIdentity.properties.clientId
output uiPrincipalId string = uiIdentity.properties.principalId
