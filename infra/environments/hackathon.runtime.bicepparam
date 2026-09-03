using '../resource-group.bicep'

param location = 'eastus2'
param environmentName = 'hackathon'
param deploymentCommitSha = '0000000000000000000000000000000000000000'
param deploymentWorkflowRunId = 'replace-workflow-run-id'
param deployContainerApps = false
param exposePublicUi = false
param deployRuntimeAccess = false
param semanticCacheEnabled = false

// Slice 11C must replace these non-deployable digests and model placeholders.
param uiAuthClientId = '00000000-0000-0000-0000-000000000000'
param uiAuthTenantId = '00000000-0000-0000-0000-000000000000'
param apiImageDigest = 'sha256:0000000000000000000000000000000000000000000000000000000000000000'
param uiImageDigest = 'sha256:0000000000000000000000000000000000000000000000000000000000000000'
param foundryBaseUrl = 'https://replace-before-deployment.openai.azure.com/openai/v1'
param foundrySmallDeployment = 'replace-small-deployment'
param foundrySmallModel = 'replace-small-model'
param foundrySmallModelVersion = 'replace-small-model-version'
param foundryStrongDeployment = 'replace-strong-deployment'
param foundryStrongModel = 'replace-strong-model'
param foundryStrongModelVersion = 'replace-strong-model-version'
param productionEvaluatorMode = 'LLM_JUDGE'
param judgeDeployment = 'replace-judge-deployment'
param judgeModel = 'replace-judge-model'
param judgeModelVersion = 'replace-judge-model-version'
param judgeTimeoutSeconds = 30
param foundryTokenScope = 'https://cognitiveservices.azure.com/.default'
param pricingCatalogVersion = 'replace-pricing-catalog-version'
param pricingCurrency = 'USD'
param pricingSmallInputRatePerMillionTokens = 'replace-small-input-rate'
param pricingSmallOutputRatePerMillionTokens = 'replace-small-output-rate'
param pricingSmallCachedInputRatePerMillionTokens = null
param pricingStrongInputRatePerMillionTokens = 'replace-strong-input-rate'
param pricingStrongOutputRatePerMillionTokens = 'replace-strong-output-rate'
param pricingStrongCachedInputRatePerMillionTokens = null
param pricingJudgeInputRatePerMillionTokens = 'replace-judge-input-rate'
param pricingJudgeOutputRatePerMillionTokens = 'replace-judge-output-rate'
param pricingJudgeCachedInputRatePerMillionTokens = null

param applicationInsightsSamplingRatio = '0.25'
