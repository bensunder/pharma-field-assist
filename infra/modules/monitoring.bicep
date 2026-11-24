param prefix string
param location string
param tags object
param oncallEmail string
param retentionDays int

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${prefix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: retentionDays
  }
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${prefix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
    DisableLocalAuth: true // ingestion via Entra auth only
    IngestionMode: 'LogAnalytics'
  }
}

resource ag 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-${prefix}-oncall'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'pfa-oncall'
    enabled: true
    emailReceivers: [
      {
        name: 'oncall'
        emailAddress: oncallEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

output logAnalyticsId string = law.id
output logAnalyticsName string = law.name
output logAnalyticsCustomerId string = law.properties.customerId
output appInsightsId string = appi.id
output actionGroupId string = ag.id
