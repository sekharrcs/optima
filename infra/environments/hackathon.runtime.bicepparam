using '../resource-group.bicep'

param location = 'eastus'
param environmentName = 'hackathon'
param deployContainerApps = false
param deployRuntimeAccess = false

// Slice 11B/11C must replace these deployment gates with reviewed build/model values.
param imageTag = 'slice-11a-unbuilt'
param foundryBaseUrl = 'https://replace-before-deployment.openai.azure.com/openai/v1'
param foundrySmallDeployment = 'replace-small-deployment'
param foundryStrongDeployment = 'replace-strong-deployment'
param foundryTokenScope = 'https://cognitiveservices.azure.com/.default'
param redisEmbeddingDeployment = 'replace-embedding-deployment'
param redisEmbeddingModel = 'replace-embedding-model'
param redisEmbeddingDimension = 1536

param applicationInsightsSamplingRatio = '0.25'