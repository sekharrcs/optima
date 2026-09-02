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

@description('Full Git commit SHA shared by both production images.')
@minLength(40)
@maxLength(40)
param deploymentCommitSha string

@description('GitHub Actions workflow run identifier recorded on the deployed revisions.')
param deploymentWorkflowRunId string

@description('Deploy API and UI only after images, runtime composition, and data-plane access are ready.')
param deployContainerApps bool = false

@description('Expose the UI only after its deployed Entra authentication configuration is verified.')
param exposePublicUi bool = false

@description('Create OPTIMA-owned runtime access assignments. Requires Azure RBAC administration on the registry.')
param deployRuntimeAccess bool = false

@description('Provision and configure the semantic-cache infrastructure and runtime integration.')
param semanticCacheEnabled bool

@description('Existing single-tenant Microsoft Entra application client ID for UI authentication.')
param uiAuthClientId string

@description('Microsoft Entra tenant ID that may authenticate to the public UI.')
param uiAuthTenantId string

@secure()
@description('Confidential-client secret of the existing UI Entra app registration. Supplied at preflight; never committed to source or parameter files.')
param uiAuthClientSecret string = ''

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

@description('Provider model identity expected from the OPTIMA SMALL deployment.')
param foundrySmallModel string

@description('Foundry deployment mapped to the OPTIMA STRONG role.')
param foundryStrongDeployment string

@description('Provider model identity expected from the OPTIMA STRONG deployment.')
param foundryStrongModel string

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
param redisEmbeddingDeployment string?

@description('Provider-reported embedding model identity.')
param redisEmbeddingModel string?

@description('Embedding vector dimension shared by Foundry and RediSearch.')
param redisEmbeddingDimension int?

@description('Root trace sampling ratio for Application Insights.')
@allowed([
  '0.1'
  '0.25'
  '0.5'
  '1.0'
])
param applicationInsightsSamplingRatio string = '0.25'

@description('Reviewed pricing catalog version that identifies the exact model-rate source.')
param pricingCatalogVersion string

@description('Shared ISO 4217 currency used by every reviewed model rate.')
param pricingCurrency string

@description('Reviewed SMALL input price per million tokens.')
param pricingSmallInputRatePerMillionTokens string

@description('Reviewed SMALL output price per million tokens.')
param pricingSmallOutputRatePerMillionTokens string

@description('Reviewed SMALL cached-input price per million tokens when the selected model has a distinct rate.')
param pricingSmallCachedInputRatePerMillionTokens string?

@description('Reviewed STRONG input price per million tokens.')
param pricingStrongInputRatePerMillionTokens string

@description('Reviewed STRONG output price per million tokens.')
param pricingStrongOutputRatePerMillionTokens string

@description('Reviewed STRONG cached-input price per million tokens when the selected model has a distinct rate.')
param pricingStrongCachedInputRatePerMillionTokens string?

@description('Reviewed JUDGE input price per million tokens in LLM_JUDGE mode.')
param pricingJudgeInputRatePerMillionTokens string?

@description('Reviewed JUDGE output price per million tokens in LLM_JUDGE mode.')
param pricingJudgeOutputRatePerMillionTokens string?

@description('Reviewed JUDGE cached-input price per million tokens when the selected model has a distinct rate.')
param pricingJudgeCachedInputRatePerMillionTokens string?

@description('Reviewed embedding input price per million tokens.')
param pricingEmbeddingInputRatePerMillionTokens string?

var cacheConfigurationIsComplete = !empty(trim(redisEmbeddingDeployment ?? '')) && !startsWith(
  toLower(redisEmbeddingDeployment ?? ''),
  'replace-'
) && !empty(trim(redisEmbeddingModel ?? '')) && !startsWith(toLower(redisEmbeddingModel ?? ''), 'replace-') && (redisEmbeddingDimension ?? 0) >= 1 && (redisEmbeddingDimension ?? 0) <= 32768 && !empty(trim(pricingEmbeddingInputRatePerMillionTokens ?? '')) && !startsWith(
  toLower(pricingEmbeddingInputRatePerMillionTokens ?? ''),
  'replace-'
)
var cacheConfigurationIsAbsent = redisEmbeddingDeployment == null && redisEmbeddingModel == null && redisEmbeddingDimension == null && pricingEmbeddingInputRatePerMillionTokens == null
var validatedSemanticCacheEnabled = semanticCacheEnabled
  ? cacheConfigurationIsComplete
      ? true
      : fail('Enabled semantic cache requires deployable embedding deployment, model, dimension, and reviewed input pricing.')
  : cacheConfigurationIsAbsent
      ? false
      : fail('Disabled semantic cache rejects Redis, embedding, and embedding-pricing parameters.')
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
    deploymentCommitSha: deploymentCommitSha
    deploymentWorkflowRunId: deploymentWorkflowRunId
    deployContainerApps: deployContainerApps
    deployRuntimeAccess: deployRuntimeAccess
    environmentName: environmentName
    exposePublicUi: exposePublicUi
    foundryBaseUrl: foundryBaseUrl
    foundrySmallDeployment: foundrySmallDeployment
    foundrySmallModel: foundrySmallModel
    foundryStrongDeployment: foundryStrongDeployment
    foundryStrongModel: foundryStrongModel
    foundryTokenScope: foundryTokenScope
    judgeDeployment: judgeDeployment
    judgeModel: judgeModel
    judgeTimeoutSeconds: judgeTimeoutSeconds
    location: location
    pricingCatalogVersion: pricingCatalogVersion
    pricingCurrency: pricingCurrency
    pricingEmbeddingInputRatePerMillionTokens: pricingEmbeddingInputRatePerMillionTokens
    pricingJudgeCachedInputRatePerMillionTokens: pricingJudgeCachedInputRatePerMillionTokens
    pricingJudgeInputRatePerMillionTokens: pricingJudgeInputRatePerMillionTokens
    pricingJudgeOutputRatePerMillionTokens: pricingJudgeOutputRatePerMillionTokens
    pricingSmallCachedInputRatePerMillionTokens: pricingSmallCachedInputRatePerMillionTokens
    pricingSmallInputRatePerMillionTokens: pricingSmallInputRatePerMillionTokens
    pricingSmallOutputRatePerMillionTokens: pricingSmallOutputRatePerMillionTokens
    pricingStrongCachedInputRatePerMillionTokens: pricingStrongCachedInputRatePerMillionTokens
    pricingStrongInputRatePerMillionTokens: pricingStrongInputRatePerMillionTokens
    pricingStrongOutputRatePerMillionTokens: pricingStrongOutputRatePerMillionTokens
    productionEvaluatorMode: productionEvaluatorMode
    redisEmbeddingDeployment: redisEmbeddingDeployment
    redisEmbeddingDimension: redisEmbeddingDimension
    redisEmbeddingModel: redisEmbeddingModel
    semanticCacheEnabled: validatedSemanticCacheEnabled
    uiAuthClientId: uiAuthClientId
    uiAuthClientSecret: uiAuthClientSecret
    uiAuthTenantId: uiAuthTenantId
    uiImageDigest: validatedUiImageDigest
  }
}

output resourceGroupName string = resourceGroup.name
output resourceGroupId string = resourceGroup.id
output registryName string = resources.outputs.registryName
output registryLoginServer string = resources.outputs.registryLoginServer
output containerAppsEnvironmentDefaultDomain string = resources.outputs.containerAppsEnvironmentDefaultDomain
output apiContainerAppName string = resources.outputs.apiContainerAppName
output apiRevisionName string = resources.outputs.apiRevisionName
output apiUrl string = resources.outputs.apiUrl
output uiContainerAppName string = resources.outputs.uiContainerAppName
output uiRevisionName string = resources.outputs.uiRevisionName
output uiUrl string = resources.outputs.uiUrl
output cosmosAccountName string = resources.outputs.cosmosAccountName
output cosmosEndpoint string = resources.outputs.cosmosEndpoint
output cosmosDatabaseName string = resources.outputs.cosmosDatabaseName
output cosmosContainerName string = resources.outputs.cosmosContainerName
output redisName string? = resources.outputs.?redisName
output redisHost string? = resources.outputs.?redisHost
output redisPort int? = resources.outputs.?redisPort
output redisIndexName string? = resources.outputs.?redisIndexName
output applicationInsightsName string = resources.outputs.applicationInsightsName
output apiIdentityResourceId string = resources.outputs.apiIdentityResourceId
output apiIdentityPrincipalId string = resources.outputs.apiIdentityPrincipalId
output uiIdentityResourceId string = resources.outputs.uiIdentityResourceId
output uiIdentityPrincipalId string = resources.outputs.uiIdentityPrincipalId
