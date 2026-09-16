@description('Azure region code for monitoring resources.')
param location string

@description('Log Analytics workspace name.')
param logAnalyticsWorkspaceName string

@description('Application Insights component name.')
param applicationInsightsName string

@description('Common resource tags.')
param tags object

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  tags: tags
  properties: {
    features: {
      disableLocalAuth: true
      enableDataExport: false
      enableLogAccessUsingOnlyResourcePermissions: true
      immediatePurgeDataOn30Days: true
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
    retentionInDays: 30
    sku: {
      name: 'PerGB2018'
    }
    workspaceCapping: {
      dailyQuotaGb: json('0.25')
    }
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: applicationInsightsName
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    DisableIpMasking: false
    DisableLocalAuth: false
    ForceCustomerStorageForProfiler: false
    ImmediatePurgeDataOn30Days: true
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
    RetentionInDays: 30
    SamplingPercentage: 100
    WorkspaceResourceId: workspace.id
  }
}

// Adopts the Application Insights auto-created Smart Detection action group as a
// managed foundation resource. No Failure Anomalies alert rule is declared and
// Microsoft.AlertsManagement is intentionally left unregistered.
resource smartDetectionActionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'Application Insights Smart Detection'
  location: 'global'
  properties: {
    enabled: true
    groupShortName: 'SmartDetect'
    armRoleReceivers: [
      {
        name: 'Monitoring Contributor'
        roleId: '749f88d5-cbae-40b8-bcfc-e573ddc772fa'
        useCommonAlertSchema: true
      }
      {
        name: 'Monitoring Reader'
        roleId: '43d0d8ad-25c7-4714-9337-8ba259a9fe05'
        useCommonAlertSchema: true
      }
    ]
    automationRunbookReceivers: []
    azureAppPushReceivers: []
    azureFunctionReceivers: []
    emailReceivers: []
    eventHubReceivers: []
    itsmReceivers: []
    logicAppReceivers: []
    smsReceivers: []
    voiceReceivers: []
    webhookReceivers: []
  }
}

output workspaceResourceId string = workspace.id
output applicationInsightsResourceId string = applicationInsights.id
output smartDetectionActionGroupResourceId string = smartDetectionActionGroup.id
@secure()
output connectionString string = applicationInsights.properties.ConnectionString
