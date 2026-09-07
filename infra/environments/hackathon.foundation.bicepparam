using '../resource-group.bicep'

param location = 'eastus2'
param environmentName = 'hackathon'
param deploymentCommitSha = '0000000000000000000000000000000000000000'
param deploymentWorkflowRunId = 'replace-workflow-run-id'
param semanticCacheEnabled = false
