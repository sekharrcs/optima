targetScope = 'subscription'

@description('Azure region code used by all OPTIMA resources.')
@allowed([
  'eastus2'
])
param location string = 'eastus2'

@description('Deployment environment name.')
@allowed([
  'hackathon'
])
param environmentName string = 'hackathon'

@description('Deploy API and UI only after images, runtime composition, and data-plane access are ready.')
param deployContainerApps bool = false

@description('Create OPTIMA-owned runtime access assignments. Requires Azure RBAC administration on the registry.')
param deployRuntimeAccess bool = false

@description('Existing single-tenant Microsoft Entra application client ID for UI authentication.')
param uiAuthClientId string

@description('Microsoft Entra tenant ID that may authenticate to the public UI.')
param uiAuthTenantId string

@description('Immutable API image manifest digest produced by a later build slice.')
@minLength(71)
@maxLength(71)
param apiImageDigest string

@description('Immutable UI image manifest digest produced by a later build slice.')
@minLength(71)
@maxLength(71)
param uiImageDigest string

@description('Foundry or APIM Azure OpenAI v1 API root.')
param foundryBaseUrl string

@description('Foundry deployment mapped to the OPTIMA SMALL role.')
param foundrySmallDeployment string

@description('Foundry deployment mapped to the OPTIMA STRONG role.')
param foundryStrongDeployment string

@description('Production quality evaluator mode.')
@allowed([
  'EXACT_REFERENCE'
  'LLM_JUDGE'
])
param productionEvaluatorMode string

@description('Foundry deployment mapped to the OPTIMA JUDGE role in LLM_JUDGE mode.')
param judgeDeployment string?

@description('Provider model identity expected for the OPTIMA JUDGE role in LLM_JUDGE mode.')
param judgeModel string?

@description('Timeout in seconds for one JUDGE model request.')
@minValue(1)
@maxValue(120)
param judgeTimeoutSeconds int = 30

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

var placeholderImageDigest = 'sha256:0000000000000000000000000000000000000000000000000000000000000000'
var apiDigestHex = substring(apiImageDigest, 7, 64)
var uiDigestHex = substring(uiImageDigest, 7, 64)
var apiImageDigestIsDeployable = startsWith(apiImageDigest, 'sha256:') && apiImageDigest == toLower(apiImageDigest) && empty(replace(
  replace(
    replace(
      replace(
        replace(
          replace(
            replace(
              replace(
                replace(
                  replace(
                    replace(
                      replace(
                        replace(replace(replace(replace(apiDigestHex, '0', ''), '1', ''), '2', ''), '3', ''),
                        '4',
                        ''
                      ),
                      '5',
                      ''
                    ),
                    '6',
                    ''
                  ),
                  '7',
                  ''
                ),
                '8',
                ''
              ),
              '9',
              ''
            ),
            'a',
            ''
          ),
          'b',
          ''
        ),
        'c',
        ''
      ),
      'd',
      ''
    ),
    'e',
    ''
  ),
  'f',
  ''
)) && apiImageDigest != placeholderImageDigest
var uiImageDigestIsDeployable = startsWith(uiImageDigest, 'sha256:') && uiImageDigest == toLower(uiImageDigest) && empty(replace(
  replace(
    replace(
      replace(
        replace(
          replace(
            replace(
              replace(
                replace(
                  replace(
                    replace(
                      replace(
                        replace(replace(replace(replace(uiDigestHex, '0', ''), '1', ''), '2', ''), '3', ''),
                        '4',
                        ''
                      ),
                      '5',
                      ''
                    ),
                    '6',
                    ''
                  ),
                  '7',
                  ''
                ),
                '8',
                ''
              ),
              '9',
              ''
            ),
            'a',
            ''
          ),
          'b',
          ''
        ),
        'c',
        ''
      ),
      'd',
      ''
    ),
    'e',
    ''
  ),
  'f',
  ''
)) && uiImageDigest != placeholderImageDigest
var validatedApiImageDigest = !deployContainerApps || apiImageDigestIsDeployable
  ? apiImageDigest
  : fail('Container Apps deployment requires a non-placeholder API sha256 digest.')
var validatedUiImageDigest = !deployContainerApps || uiImageDigestIsDeployable
  ? uiImageDigest
  : fail('Container Apps deployment requires a non-placeholder UI sha256 digest.')
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
    apiImageDigest: validatedApiImageDigest
    deployContainerApps: deployContainerApps
    deployRuntimeAccess: deployRuntimeAccess
    environmentName: environmentName
    foundryBaseUrl: foundryBaseUrl
    foundrySmallDeployment: foundrySmallDeployment
    foundryStrongDeployment: foundryStrongDeployment
    foundryTokenScope: foundryTokenScope
    judgeDeployment: judgeDeployment
    judgeModel: judgeModel
    judgeTimeoutSeconds: judgeTimeoutSeconds
    location: location
    productionEvaluatorMode: productionEvaluatorMode
    redisEmbeddingDeployment: redisEmbeddingDeployment
    redisEmbeddingDimension: redisEmbeddingDimension
    redisEmbeddingModel: redisEmbeddingModel
    uiAuthClientId: uiAuthClientId
    uiAuthTenantId: uiAuthTenantId
    uiImageDigest: validatedUiImageDigest
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
