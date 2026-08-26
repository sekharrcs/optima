@description('Azure region code for Container Apps.')
param location string

@description('Deployment environment name.')
param environmentName string

@description('Container Apps managed environment name.')
param containerAppsEnvironmentName string

@description('API Container App name.')
param apiContainerAppName string

@description('UI Container App name.')
param uiContainerAppName string

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

@description('Cosmos DB HTTPS account endpoint.')
param cosmosEndpoint string

@description('Cosmos DB database name.')
param cosmosDatabaseName string

@description('Cosmos DB run-history container name.')
param cosmosContainerName string

@description('Azure Managed Redis hostname.')
param redisHost string

@description('Pre-provisioned RediSearch index name.')
param redisIndexName string

@description('Embedding vector dimension shared by Foundry and RediSearch.')
param redisEmbeddingDimension int

@description('Provider-reported embedding model identity.')
param redisEmbeddingModel string

@description('Foundry embedding deployment used by the semantic cache.')
param redisEmbeddingDeployment string

@description('Foundry or APIM Azure OpenAI v1 API root.')
param foundryBaseUrl string

@description('Foundry deployment mapped to the OPTIMA SMALL role.')
param foundrySmallDeployment string

@description('Foundry deployment mapped to the OPTIMA STRONG role.')
param foundryStrongDeployment string

@description('OAuth token scope accepted by the configured Foundry or APIM endpoint.')
param foundryTokenScope string

@description('Application Insights connection string.')
param applicationInsightsConnectionString string

@description('Root trace sampling ratio for Application Insights.')
param applicationInsightsSamplingRatio string

@description('Common resource tags.')
param tags object

resource managedEnvironment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: containerAppsEnvironmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'none'
    }
    publicNetworkAccess: 'Enabled'
    zoneRedundant: false
  }
}

resource api 'Microsoft.App/containerApps@2025-07-01' = {
  name: apiContainerAppName
  location: location
  tags: tags
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
      registries: [
        {
          identity: apiIdentityResourceId
          server: registryLoginServer
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          env: [
            {
              name: 'OPTIMA_DEPLOYMENT_ENVIRONMENT'
              value: environmentName
            }
            {
              name: 'OPTIMA_PRODUCTION_EVALUATOR_MODE'
              value: 'EXACT_REFERENCE'
            }
            {
              name: 'OPTIMA_PRODUCTION_REQUIRE_REFERENCE_OUTPUT'
              value: 'true'
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
              name: 'OPTIMA_FOUNDRY_STRONG_DEPLOYMENT'
              value: foundryStrongDeployment
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
              name: 'OPTIMA_REDIS_HOST'
              value: redisHost
            }
            {
              name: 'OPTIMA_REDIS_INDEX_NAME'
              value: redisIndexName
            }
            {
              name: 'OPTIMA_REDIS_EMBEDDING_DIMENSION'
              value: string(redisEmbeddingDimension)
            }
            {
              name: 'OPTIMA_REDIS_EMBEDDING_MODEL'
              value: redisEmbeddingModel
            }
            {
              name: 'OPTIMA_REDIS_EMBEDDING_DEPLOYMENT'
              value: redisEmbeddingDeployment
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
              name: 'OPTIMA_APPLICATION_INSIGHTS_ENABLED'
              value: 'true'
            }
            {
              name: 'OPTIMA_APPLICATION_INSIGHTS_CONNECTION_STRING'
              value: applicationInsightsConnectionString
            }
            {
              name: 'OPTIMA_APPLICATION_INSIGHTS_SERVICE_NAME'
              value: 'optima-api'
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
          ]
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

resource ui 'Microsoft.App/containerApps@2025-07-01' = {
  name: uiContainerAppName
  location: location
  tags: tags
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
        external: true
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
          name: 'ui'
          image: uiImage
          env: [
            {
              name: 'OPTIMA_DEPLOYMENT_ENVIRONMENT'
              value: environmentName
            }
            {
              name: 'OPTIMA_REQUIRE_REFERENCE_OUTPUT'
              value: 'true'
            }
            {
              name: 'OPTIMA_API_BASE_URL'
              value: 'https://${api.properties.configuration.ingress.fqdn}'
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

output environmentResourceId string = managedEnvironment.id
output apiResourceId string = api.id
output apiUrl string = 'https://${api.properties.configuration.ingress.fqdn}'
output uiResourceId string = ui.id
output uiUrl string = 'https://${ui.properties.configuration.ingress.fqdn}'
