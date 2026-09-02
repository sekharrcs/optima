@description('Azure region code for Container Apps.')
param location string

@description('Deployment environment name.')
param environmentName string

@description('Full Git commit SHA shared by both production images.')
param deploymentCommitSha string

@description('GitHub Actions workflow run identifier recorded on the deployed revisions.')
param deploymentWorkflowRunId string

@description('W3C traceparent the pre-exposure smoke job sends so its API request is correlated in Application Insights.')
param smokeTraceparent string

@description('Unique per-run marker the pre-exposure smoke job records on its request.')
param smokeRunMarker string

@description('Deploy API and UI only after publication, access, and authentication gates pass.')
param deployApplications bool

@description('Expose UI ingress only after the deployed authentication child resource is verified.')
param exposePublicUi bool

@description('Configure the API semantic-cache runtime integration.')
param semanticCacheEnabled bool

@description('Container Apps managed environment name.')
param containerAppsEnvironmentName string

@description('API Container App name.')
param apiContainerAppName string

@description('UI Container App name.')
param uiContainerAppName string

@description('Pre-exposure deployment smoke Container Apps job name.')
param smokeJobName string

@description('Immutable API image reference.')
param apiImage string

@description('Immutable UI image reference.')
param uiImage string

@description('Azure Container Registry login server.')
param registryLoginServer string

@description('API user-assigned managed identity resource ID.')
param apiIdentityResourceId string

@description('API user-assigned managed identity client ID.')
param apiIdentityClientId string

@description('API user-assigned managed identity object ID.')
param apiIdentityPrincipalId string

@description('UI user-assigned managed identity resource ID.')
param uiIdentityResourceId string

@description('Existing single-tenant Microsoft Entra application client ID for UI authentication.')
param uiAuthClientId string

@description('Microsoft Entra tenant ID that may authenticate to the public UI.')
param uiAuthTenantId string

@secure()
@description('Confidential-client secret of the existing UI Entra app registration. Enables the authorization-code (hybrid) flow instead of the implicit flow. Supplied at preflight; never committed.')
param uiAuthClientSecret string

@description('Cosmos DB HTTPS account endpoint.')
param cosmosEndpoint string

@description('Cosmos DB database name.')
param cosmosDatabaseName string

@description('Cosmos DB run-history container name.')
param cosmosContainerName string

@description('Azure Managed Redis hostname.')
param redisHost string?

@description('Pre-provisioned RediSearch index name.')
param redisIndexName string?

@description('Embedding vector dimension shared by Foundry and RediSearch.')
param redisEmbeddingDimension int?

@description('Provider-reported embedding model identity.')
param redisEmbeddingModel string?

@description('Foundry embedding deployment used by the semantic cache.')
param redisEmbeddingDeployment string?

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
param judgeTimeoutSeconds int

@description('OAuth token scope accepted by the configured Foundry or APIM endpoint.')
param foundryTokenScope string

@secure()
@description('Application Insights connection string stored as a Container App secret.')
param applicationInsightsConnectionString string

@description('Root trace sampling ratio for Application Insights.')
param applicationInsightsSamplingRatio string

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

@description('Common resource tags.')
param tags object

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
      : fail('LLM_JUDGE Container Apps deployment requires deployable judge identity and pricing values.')
  : judgeConfigurationIsAbsent
      ? productionEvaluatorMode
      : fail('EXACT_REFERENCE Container Apps deployment rejects inactive JUDGE identity and pricing values.')
var judgeEnvironment = validatedEvaluatorMode == 'LLM_JUDGE'
  ? concat(
      [
        {
          name: 'OPTIMA_JUDGE_DEPLOYMENT'
          value: judgeDeployment!
        }
        {
          name: 'OPTIMA_JUDGE_MODEL'
          value: judgeModel!
        }
        {
          name: 'OPTIMA_JUDGE_TIMEOUT_SECONDS'
          value: string(judgeTimeoutSeconds)
        }
        {
          name: 'OPTIMA_PRICING_JUDGE_INPUT_RATE_PER_MILLION_TOKENS'
          value: pricingJudgeInputRatePerMillionTokens!
        }
        {
          name: 'OPTIMA_PRICING_JUDGE_OUTPUT_RATE_PER_MILLION_TOKENS'
          value: pricingJudgeOutputRatePerMillionTokens!
        }
      ],
      !empty(pricingJudgeCachedInputRatePerMillionTokens)
        ? [
            {
              name: 'OPTIMA_PRICING_JUDGE_CACHED_INPUT_RATE_PER_MILLION_TOKENS'
              value: pricingJudgeCachedInputRatePerMillionTokens!
            }
          ]
        : []
    )
  : []
var semanticCacheConfigurationIsComplete = !empty(trim(redisHost ?? '')) && !empty(trim(redisIndexName ?? '')) && !empty(trim(redisEmbeddingDeployment ?? '')) && !startsWith(
  toLower(redisEmbeddingDeployment ?? ''),
  'replace-'
) && !empty(trim(redisEmbeddingModel ?? '')) && !startsWith(toLower(redisEmbeddingModel ?? ''), 'replace-') && (redisEmbeddingDimension ?? 0) >= 1 && (redisEmbeddingDimension ?? 0) <= 32768 && !empty(trim(pricingEmbeddingInputRatePerMillionTokens ?? '')) && !startsWith(
  toLower(pricingEmbeddingInputRatePerMillionTokens ?? ''),
  'replace-'
)
var semanticCacheConfigurationIsAbsent = redisHost == null && redisIndexName == null && redisEmbeddingDeployment == null && redisEmbeddingModel == null && redisEmbeddingDimension == null && pricingEmbeddingInputRatePerMillionTokens == null
var validatedSemanticCacheEnabled = semanticCacheEnabled
  ? semanticCacheConfigurationIsComplete
      ? true
      : fail('Enabled semantic cache requires Redis, embedding, and reviewed embedding-pricing values.')
  : semanticCacheConfigurationIsAbsent
      ? false
      : fail('Disabled semantic cache rejects Redis, embedding, and embedding-pricing values.')
var semanticCacheEnvironment = validatedSemanticCacheEnabled
  ? [
      {
        name: 'OPTIMA_REDIS_HOST'
        value: redisHost!
      }
      {
        name: 'OPTIMA_REDIS_INDEX_NAME'
        value: redisIndexName!
      }
      {
        name: 'OPTIMA_REDIS_EMBEDDING_DIMENSION'
        value: string(redisEmbeddingDimension!)
      }
      {
        name: 'OPTIMA_REDIS_EMBEDDING_MODEL'
        value: redisEmbeddingModel!
      }
      {
        name: 'OPTIMA_REDIS_EMBEDDING_DEPLOYMENT'
        value: redisEmbeddingDeployment!
      }
      {
        name: 'OPTIMA_REDIS_AUTH_MODE'
        value: 'MANAGED_IDENTITY'
      }
      {
        name: 'OPTIMA_REDIS_OBJECT_ID'
        value: apiIdentityPrincipalId
      }
      {
        name: 'OPTIMA_REDIS_MANAGED_IDENTITY_CLIENT_ID'
        value: apiIdentityClientId
      }
      {
        name: 'OPTIMA_PRICING_EMBEDDING_INPUT_RATE_PER_MILLION_TOKENS'
        value: pricingEmbeddingInputRatePerMillionTokens!
      }
    ]
  : []
var optionalPricingEnvironment = concat(
  !empty(pricingSmallCachedInputRatePerMillionTokens)
    ? [
        {
          name: 'OPTIMA_PRICING_SMALL_CACHED_INPUT_RATE_PER_MILLION_TOKENS'
          value: pricingSmallCachedInputRatePerMillionTokens!
        }
      ]
    : [],
  !empty(pricingStrongCachedInputRatePerMillionTokens)
    ? [
        {
          name: 'OPTIMA_PRICING_STRONG_CACHED_INPUT_RATE_PER_MILLION_TOKENS'
          value: pricingStrongCachedInputRatePerMillionTokens!
        }
      ]
    : []
)
var deploymentTags = union(tags, {
  sourceCommit: deploymentCommitSha
  workflowRun: deploymentWorkflowRunId
})
var revisionSuffix = 'r-${take(deploymentCommitSha, 12)}-${take(deploymentWorkflowRunId, 20)}'

resource managedEnvironment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: containerAppsEnvironmentName
  location: location
  tags: deploymentTags
  properties: {
    appLogsConfiguration: {
      destination: 'none'
    }
    publicNetworkAccess: 'Enabled'
    zoneRedundant: false
  }
}

resource api 'Microsoft.App/containerApps@2025-07-01' = if (deployApplications) {
  name: apiContainerAppName
  location: location
  tags: deploymentTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${apiIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: false
        targetPort: 8000
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
        transport: 'auto'
      }
      maxInactiveRevisions: 2
      secrets: [
        {
          name: 'application-insights-connection-string'
          value: applicationInsightsConnectionString
        }
      ]
      registries: [
        {
          identity: apiIdentityResourceId
          server: registryLoginServer
        }
      ]
    }
    template: {
      revisionSuffix: revisionSuffix
      containers: [
        {
          name: 'api'
          image: apiImage
          env: concat(
            [
              {
                name: 'OPTIMA_DEPLOYMENT_ENVIRONMENT'
                value: environmentName
              }
              {
                name: 'OPTIMA_PRODUCTION_EVALUATOR_MODE'
                value: validatedEvaluatorMode
              }
              {
                name: 'OPTIMA_PRODUCTION_REQUIRE_REFERENCE_OUTPUT'
                value: validatedEvaluatorMode == 'EXACT_REFERENCE' ? 'true' : 'false'
              }
              {
                name: 'OPTIMA_SEMANTIC_CACHE_ENABLED'
                value: validatedSemanticCacheEnabled ? 'true' : 'false'
              }
              {
                name: 'OPTIMA_EXECUTION_CONCURRENCY_LIMIT'
                value: '4'
              }
              {
                name: 'OPTIMA_EXECUTION_TIMEOUT_SECONDS'
                value: '300'
              }
              {
                name: 'OPTIMA_FOUNDRY_BASE_URL'
                value: foundryBaseUrl
              }
              {
                name: 'OPTIMA_FOUNDRY_SMALL_DEPLOYMENT'
                value: foundrySmallDeployment
              }
              {
                name: 'OPTIMA_FOUNDRY_SMALL_MODEL'
                value: foundrySmallModel
              }
              {
                name: 'OPTIMA_FOUNDRY_STRONG_DEPLOYMENT'
                value: foundryStrongDeployment
              }
              {
                name: 'OPTIMA_FOUNDRY_STRONG_MODEL'
                value: foundryStrongModel
              }
              {
                name: 'OPTIMA_FOUNDRY_AUTH_MODE'
                value: 'MANAGED_IDENTITY'
              }
              {
                name: 'OPTIMA_FOUNDRY_TOKEN_SCOPE'
                value: foundryTokenScope
              }
              {
                name: 'OPTIMA_FOUNDRY_MANAGED_IDENTITY_CLIENT_ID'
                value: apiIdentityClientId
              }
              {
                name: 'OPTIMA_COSMOS_ENDPOINT'
                value: cosmosEndpoint
              }
              {
                name: 'OPTIMA_COSMOS_DATABASE_NAME'
                value: cosmosDatabaseName
              }
              {
                name: 'OPTIMA_COSMOS_CONTAINER_NAME'
                value: cosmosContainerName
              }
              {
                name: 'OPTIMA_COSMOS_AUTH_MODE'
                value: 'MANAGED_IDENTITY'
              }
              {
                name: 'OPTIMA_COSMOS_MANAGED_IDENTITY_CLIENT_ID'
                value: apiIdentityClientId
              }
              {
                name: 'OPTIMA_COSMOS_TIMEOUT_SECONDS'
                value: '10'
              }
              {
                name: 'OPTIMA_APPLICATION_INSIGHTS_ENABLED'
                value: 'true'
              }
              {
                name: 'OPTIMA_APPLICATION_INSIGHTS_CONNECTION_STRING'
                secretRef: 'application-insights-connection-string'
              }
              {
                name: 'OPTIMA_APPLICATION_INSIGHTS_SERVICE_NAME'
                value: 'optima-api'
              }
              {
                name: 'OPTIMA_APPLICATION_INSIGHTS_SERVICE_VERSION'
                value: deploymentCommitSha
              }
              {
                name: 'OPTIMA_APPLICATION_INSIGHTS_DEPLOYMENT_ENVIRONMENT'
                value: environmentName
              }
              {
                name: 'OPTIMA_APPLICATION_INSIGHTS_SAMPLING_RATIO'
                value: applicationInsightsSamplingRatio
              }
              {
                name: 'OPTIMA_APPLICATION_INSIGHTS_LIVE_METRICS_ENABLED'
                value: 'false'
              }
              {
                name: 'OPTIMA_APPLICATION_INSIGHTS_PERFORMANCE_COUNTERS_ENABLED'
                value: 'false'
              }
              {
                name: 'OPTIMA_APPLICATION_INSIGHTS_OFFLINE_STORAGE_ENABLED'
                value: 'false'
              }
              {
                name: 'OPTIMA_PRODUCTION_COST_MEASUREMENT_REQUIRED'
                value: 'true'
              }
              {
                name: 'OPTIMA_PRICING_CATALOG_VERSION'
                value: pricingCatalogVersion
              }
              {
                name: 'OPTIMA_PRICING_CURRENCY'
                value: pricingCurrency
              }
              {
                name: 'OPTIMA_PRICING_SMALL_INPUT_RATE_PER_MILLION_TOKENS'
                value: pricingSmallInputRatePerMillionTokens
              }
              {
                name: 'OPTIMA_PRICING_SMALL_OUTPUT_RATE_PER_MILLION_TOKENS'
                value: pricingSmallOutputRatePerMillionTokens
              }
              {
                name: 'OPTIMA_PRICING_STRONG_INPUT_RATE_PER_MILLION_TOKENS'
                value: pricingStrongInputRatePerMillionTokens
              }
              {
                name: 'OPTIMA_PRICING_STRONG_OUTPUT_RATE_PER_MILLION_TOKENS'
                value: pricingStrongOutputRatePerMillionTokens
              }
            ],
            judgeEnvironment,
            semanticCacheEnvironment,
            optionalPricingEnvironment
          )
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/api/v1/health'
                port: 8000
                scheme: 'HTTP'
              }
              failureThreshold: 3
              initialDelaySeconds: 10
              periodSeconds: 30
              successThreshold: 1
              timeoutSeconds: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/api/v1/health'
                port: 8000
                scheme: 'HTTP'
              }
              failureThreshold: 3
              initialDelaySeconds: 5
              periodSeconds: 10
              successThreshold: 1
              timeoutSeconds: 3
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
        }
      ]
      scale: {
        maxReplicas: 3
        minReplicas: 0
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '4'
              }
            }
          }
        ]
      }
      terminationGracePeriodSeconds: 30
    }
  }
}

resource ui 'Microsoft.App/containerApps@2025-07-01' = if (deployApplications) {
  name: uiContainerAppName
  location: location
  tags: deploymentTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uiIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        allowInsecure: false
        external: exposePublicUi
        targetPort: 8501
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
        transport: 'auto'
      }
      maxInactiveRevisions: 2
      secrets: [
        {
          name: 'ui-auth-client-secret'
          value: uiAuthClientSecret
        }
      ]
      registries: [
        {
          identity: uiIdentityResourceId
          server: registryLoginServer
        }
      ]
    }
    template: {
      revisionSuffix: revisionSuffix
      containers: [
        {
          name: 'ui'
          image: uiImage
          env: [
            {
              name: 'OPTIMA_DEPLOYMENT_ENVIRONMENT'
              value: environmentName
            }
            {
              name: 'OPTIMA_UI_PRODUCTION_MODE'
              value: 'true'
            }
            {
              name: 'OPTIMA_REQUIRE_REFERENCE_OUTPUT'
              value: validatedEvaluatorMode == 'EXACT_REFERENCE' ? 'true' : 'false'
            }
            {
              name: 'OPTIMA_API_BASE_URL'
              value: 'https://${api!.properties.configuration.ingress.fqdn}'
            }
            {
              name: 'OPTIMA_API_TIMEOUT_SECONDS'
              value: '315'
            }
          ]
          probes: [
            {
              type: 'Liveness'
              tcpSocket: {
                port: 8501
              }
              failureThreshold: 3
              initialDelaySeconds: 15
              periodSeconds: 30
              successThreshold: 1
              timeoutSeconds: 3
            }
            {
              type: 'Readiness'
              tcpSocket: {
                port: 8501
              }
              failureThreshold: 3
              initialDelaySeconds: 10
              periodSeconds: 10
              successThreshold: 1
              timeoutSeconds: 3
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
        }
      ]
      scale: {
        maxReplicas: 2
        minReplicas: 0
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
      terminationGracePeriodSeconds: 30
    }
  }
}

resource uiAuthentication 'Microsoft.App/containerApps/authConfigs@2025-07-01' = if (deployApplications) {
  parent: ui
  name: 'current'
  properties: {
    globalValidation: {
      redirectToProvider: 'azureActiveDirectory'
      unauthenticatedClientAction: 'RedirectToLoginPage'
    }
    httpSettings: {
      requireHttps: true
      routes: {
        apiPrefix: '/.auth'
      }
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: uiAuthClientId
          clientSecretSettingName: 'ui-auth-client-secret'
          openIdIssuer: '${environment().authentication.loginEndpoint}${uiAuthTenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            uiAuthClientId
            'api://${uiAuthClientId}'
          ]
        }
      }
    }
    login: {
      nonce: {
        nonceExpirationInterval: '00:05:00'
        validateNonce: true
      }
      // OPTIMA only authenticates the user; it never calls downstream APIs with a
      // delegated user token, so no access/refresh token is persisted.
      tokenStore: {
        enabled: false
      }
    }
    platform: {
      enabled: true
    }
  }
}

// The UI runtime image is distroless (no shell), so the pre-exposure smoke runs
// as a one-shot Container Apps job that execs the image's own Python entrypoint
// inside the environment. `az containerapp exec` cannot target a shell-less
// container. Success is proven by the job execution exit status, not stdout,
// because the environment ships no container console logs.
resource deploymentSmokeJob 'Microsoft.App/jobs@2025-07-01' = if (deployApplications) {
  name: smokeJobName
  location: location
  tags: deploymentTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${uiIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: managedEnvironment.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 600
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      registries: [
        {
          identity: uiIdentityResourceId
          server: registryLoginServer
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'smoke'
          image: uiImage
          command: [
            'python'
          ]
          args: [
            '-m'
            'ui.deployment_smoke'
            '--traceparent'
            smokeTraceparent
            '--run-marker'
            smokeRunMarker
          ]
          env: [
            {
              name: 'OPTIMA_API_BASE_URL'
              value: 'https://${api!.properties.configuration.ingress.fqdn}'
            }
            {
              name: 'OPTIMA_API_TIMEOUT_SECONDS'
              value: '315'
            }
            {
              name: 'OPTIMA_SEMANTIC_CACHE_ENABLED'
              value: validatedSemanticCacheEnabled ? 'true' : 'false'
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
        }
      ]
    }
  }
}

output environmentResourceId string = managedEnvironment.id
output environmentDefaultDomain string = managedEnvironment.properties.defaultDomain
output apiResourceId string = deployApplications ? api!.id : ''
output apiRevisionName string = deployApplications ? api!.properties.latestRevisionName : ''
output apiUrl string = deployApplications ? 'https://${api!.properties.configuration.ingress.fqdn}' : ''
output uiResourceId string = deployApplications ? ui!.id : ''
output uiRevisionName string = deployApplications ? ui!.properties.latestRevisionName : ''
output uiUrl string = deployApplications ? 'https://${ui!.properties.configuration.ingress.fqdn}' : ''
output semanticCacheEnabled bool = validatedSemanticCacheEnabled
