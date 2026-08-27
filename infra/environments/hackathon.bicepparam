using '../main.bicep'

param location = 'eastus2'
param environmentName = 'hackathon'
param deployContainerApps = false
param deployRuntimeAccess = false

// Slice 11C must replace these non-deployable digests and model placeholders.
param apiImageDigest = 'sha256:0000000000000000000000000000000000000000000000000000000000000000'
param uiImageDigest = 'sha256:0000000000000000000000000000000000000000000000000000000000000000'
param foundryBaseUrl = 'https://replace-before-deployment.openai.azure.com/openai/v1'
param foundrySmallDeployment = 'replace-small-deployment'
param foundryStrongDeployment = 'replace-strong-deployment'
param productionEvaluatorMode = 'LLM_JUDGE'
param judgeDeployment = 'replace-judge-deployment'
param judgeModel = 'replace-judge-model'
param judgeTimeoutSeconds = 30
param foundryTokenScope = 'https://cognitiveservices.azure.com/.default'
param redisEmbeddingDeployment = 'replace-embedding-deployment'
param redisEmbeddingModel = 'replace-embedding-model'
param redisEmbeddingDimension = 1536

param applicationInsightsSamplingRatio = '0.25'
