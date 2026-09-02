targetScope = 'resourceGroup'

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

@description('W3C traceparent the pre-exposure smoke job sends so its API request is correlated in Application Insights. Required when Container Apps deploy.')
param smokeTraceparent string = ''

@description('Unique per-run marker the pre-exposure smoke job records. Required when Container Apps deploy.')
param smokeRunMarker string = ''

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
var placeholderIdentity = '00000000-0000-0000-0000-000000000000'
var uiAuthConfigurationIsDeployable = uiAuthClientId != placeholderIdentity && uiAuthTenantId != placeholderIdentity && !empty(uiAuthClientSecret)
var validatedUiAuthClientId = !deployContainerApps || uiAuthConfigurationIsDeployable
  ? uiAuthClientId
  : fail('Container Apps deployment requires a non-placeholder UI Entra client ID, tenant ID, and confidential-client secret.')
var validatedUiAuthTenantId = !deployContainerApps || uiAuthConfigurationIsDeployable
  ? uiAuthTenantId
  : fail('Container Apps deployment requires a non-placeholder UI Entra client ID, tenant ID, and confidential-client secret.')
var basePricingConfigurationIsDeployable = !empty(pricingCatalogVersion) && !startsWith(
  pricingCatalogVersion,
  'replace-'
) && !empty(pricingCurrency) && !startsWith(pricingCurrency, 'replace-') && !empty(pricingSmallInputRatePerMillionTokens) && !startsWith(
  pricingSmallInputRatePerMillionTokens,
  'replace-'
) && !empty(pricingSmallOutputRatePerMillionTokens) && !startsWith(pricingSmallOutputRatePerMillionTokens, 'replace-') && !empty(pricingStrongInputRatePerMillionTokens) && !startsWith(
  pricingStrongInputRatePerMillionTokens,
  'replace-'
) && !empty(pricingStrongOutputRatePerMillionTokens) && !startsWith(pricingStrongOutputRatePerMillionTokens, 'replace-')
var judgeConfigurationIsComplete = !empty(trim(judgeDeployment ?? '')) && !startsWith(
  toLower(judgeDeployment ?? ''),
  'replace-'
) && !empty(trim(judgeModel ?? '')) && !startsWith(toLower(judgeModel ?? ''), 'replace-') && !empty(trim(pricingJudgeInputRatePerMillionTokens ?? '')) && !startsWith(
  toLower(pricingJudgeInputRatePerMillionTokens ?? ''),
  'replace-'
) && !empty(trim(pricingJudgeOutputRatePerMillionTokens ?? '')) && !startsWith(
  toLower(pricingJudgeOutputRatePerMillionTokens ?? ''),
  'replace-'
)
var judgeConfigurationIsAbsent = judgeDeployment == null && judgeModel == null && pricingJudgeInputRatePerMillionTokens == null && pricingJudgeOutputRatePerMillionTokens == null && pricingJudgeCachedInputRatePerMillionTokens == null
var validatedEvaluatorMode = productionEvaluatorMode == 'LLM_JUDGE'
  ? judgeConfigurationIsComplete
      ? productionEvaluatorMode
      : fail('LLM_JUDGE requires deployable JUDGE identity and pricing values.')
  : judgeConfigurationIsAbsent
      ? productionEvaluatorMode
      : fail('EXACT_REFERENCE rejects inactive JUDGE identity and pricing values.')
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
var validatedPricingCatalogVersion = !deployContainerApps || basePricingConfigurationIsDeployable
  ? pricingCatalogVersion
  : fail('Container Apps deployment requires complete reviewed pricing for SMALL and STRONG roles.')
var placeholderCommitSha = '0000000000000000000000000000000000000000'
var deploymentCommitInvalidCharacters = replace(
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
                        replace(replace(replace(replace(deploymentCommitSha, '0', ''), '1', ''), '2', ''), '3', ''),
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
)
var deploymentWorkflowRunInvalidCharacters = replace(
  replace(
    replace(
      replace(
        replace(
          replace(
            replace(
              replace(replace(replace(replace(deploymentWorkflowRunId, '0', ''), '1', ''), '2', ''), '3', ''),
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
  '-',
  ''
)
var deploymentProvenanceIsDeployable = deploymentCommitSha != placeholderCommitSha && empty(deploymentCommitInvalidCharacters) && !empty(deploymentWorkflowRunId) && contains(
  deploymentWorkflowRunId,
  '-'
) && empty(deploymentWorkflowRunInvalidCharacters) && !startsWith(deploymentWorkflowRunId, 'replace-')
var validatedDeploymentCommitSha = !deployContainerApps || deploymentProvenanceIsDeployable
  ? deploymentCommitSha
  : fail('Container Apps deployment requires an exact commit SHA and workflow run ID.')
var validatedExposePublicUi = !exposePublicUi || deployContainerApps
  ? exposePublicUi
  : fail('Public UI exposure requires Container Apps deployment.')
var smokeConfigurationIsDeployable = !empty(smokeTraceparent) && !empty(smokeRunMarker)
var validatedSmokeTraceparent = !deployContainerApps || smokeConfigurationIsDeployable
  ? smokeTraceparent
  : fail('Container Apps deployment requires a pre-exposure smoke traceparent and run marker.')
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
  smokeJob: 'caj-optima-smoke-${environmentName}'
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

module redis 'modules/managed-redis.bicep' = if (validatedSemanticCacheEnabled) {
  name: 'optima-managed-redis'
  params: {
    location: location
    redisName: resourceNames.redis
    tags: tags
  }
}

module runtimeAccess 'modules/runtime-access.bicep' = if (deployRuntimeAccess) {
  name: 'optima-runtime-access'
  params: {
    apiPrincipalId: identities.outputs.apiPrincipalId
    cosmosAccountName: resourceNames.cosmosAccount
    cosmosContainerName: 'runs'
    cosmosDatabaseName: 'optima'
    redisName: validatedSemanticCacheEnabled ? resourceNames.redis : null
    registryName: resourceNames.containerRegistry
    semanticCacheEnabled: validatedSemanticCacheEnabled
    uiPrincipalId: identities.outputs.uiPrincipalId
  }
  dependsOn: [
    registry
    cosmos
    validatedSemanticCacheEnabled ? redis : cosmos
  ]
}

module containerApps 'modules/container-apps.bicep' = {
  name: 'optima-container-apps'
  params: {
    apiContainerAppName: resourceNames.apiContainerApp
    apiIdentityClientId: identities.outputs.apiClientId
    apiIdentityPrincipalId: identities.outputs.apiPrincipalId
    apiIdentityResourceId: identities.outputs.apiResourceId
    apiImage: '${registry.outputs.loginServer}/optima-api@${validatedApiImageDigest}'
    applicationInsightsConnectionString: monitoring.outputs.connectionString
    applicationInsightsSamplingRatio: applicationInsightsSamplingRatio
    containerAppsEnvironmentName: resourceNames.containerAppsEnvironment
    cosmosContainerName: cosmos.outputs.containerName
    cosmosDatabaseName: cosmos.outputs.databaseName
    cosmosEndpoint: cosmos.outputs.endpoint
    deploymentCommitSha: validatedDeploymentCommitSha
    deploymentWorkflowRunId: deploymentWorkflowRunId
    deployApplications: deployContainerApps
    environmentName: environmentName
    exposePublicUi: validatedExposePublicUi
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
    pricingCatalogVersion: validatedPricingCatalogVersion
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
    productionEvaluatorMode: validatedEvaluatorMode
    redisEmbeddingDeployment: redisEmbeddingDeployment
    redisEmbeddingDimension: redisEmbeddingDimension
    redisEmbeddingModel: redisEmbeddingModel
    redisHost: validatedSemanticCacheEnabled ? redis!.outputs.hostName : null
    redisIndexName: validatedSemanticCacheEnabled ? 'optima-cache-v1' : null
    registryLoginServer: registry.outputs.loginServer
    semanticCacheEnabled: validatedSemanticCacheEnabled
    smokeJobName: resourceNames.smokeJob
    smokeRunMarker: smokeRunMarker
    smokeTraceparent: validatedSmokeTraceparent
    tags: tags
    uiAuthClientId: validatedUiAuthClientId
    uiAuthClientSecret: uiAuthClientSecret
    uiAuthTenantId: validatedUiAuthTenantId
    uiContainerAppName: resourceNames.uiContainerApp
    uiIdentityResourceId: identities.outputs.uiResourceId
    uiImage: '${registry.outputs.loginServer}/optima-ui@${validatedUiImageDigest}'
  }
}

output registryName string = resourceNames.containerRegistry
output registryLoginServer string = registry.outputs.loginServer
output containerAppsEnvironmentDefaultDomain string = containerApps.outputs.environmentDefaultDomain
output apiContainerAppName string = resourceNames.apiContainerApp
output apiRevisionName string = deployContainerApps ? containerApps!.outputs.apiRevisionName : ''
output apiUrl string = containerApps.outputs.apiUrl
output uiContainerAppName string = resourceNames.uiContainerApp
output uiRevisionName string = deployContainerApps ? containerApps!.outputs.uiRevisionName : ''
output uiUrl string = containerApps.outputs.uiUrl
output smokeJobName string = resourceNames.smokeJob
output cosmosAccountName string = resourceNames.cosmosAccount
output cosmosEndpoint string = cosmos.outputs.endpoint
output cosmosDatabaseName string = cosmos.outputs.databaseName
output cosmosContainerName string = cosmos.outputs.containerName
output redisName string? = validatedSemanticCacheEnabled ? resourceNames.redis : null
output redisHost string? = validatedSemanticCacheEnabled ? redis!.outputs.hostName : null
output redisPort int? = validatedSemanticCacheEnabled ? redis!.outputs.port : null
output redisIndexName string? = validatedSemanticCacheEnabled ? 'optima-cache-v1' : null
output applicationInsightsName string = resourceNames.applicationInsights
output apiIdentityResourceId string = identities.outputs.apiResourceId
output apiIdentityPrincipalId string = identities.outputs.apiPrincipalId
output uiIdentityResourceId string = identities.outputs.uiResourceId
output uiIdentityPrincipalId string = identities.outputs.uiPrincipalId
