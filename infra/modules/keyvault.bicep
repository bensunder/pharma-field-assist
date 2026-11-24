// RBAC-mode Key Vault. Secret VALUES are never set here (no secrets in templates or pipeline logs):
// they are written once by a Key Vault Secrets Officer (or rotated by automation) and the container
// apps reference them by URI through the managed identity. Rotation = new secret version; ACA picks
// it up on the next revision (pipeline "restart revision" step) without a redeploy.
param prefix string
param location string
param tags object
param readerPrincipalId string
param logAnalyticsId string
param purgeProtection bool

var secretsUserRole = '4633458b-17de-408a-b874-0445c86b69e6' // Key Vault Secrets User

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: take('kv-${replace(prefix, '-', '')}${uniqueString(resourceGroup().id)}', 24)
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: purgeProtection ? true : null
    publicNetworkAccess: 'Enabled' // set 'Disabled' + private endpoint when the ACA env is VNet-integrated
    networkAcls: { defaultAction: 'Allow', bypass: 'AzureServices' }
  }
}

resource secretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, readerPrincipalId, secretsUserRole)
  scope: kv
  properties: {
    principalId: readerPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUserRole)
  }
}

resource diag 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'kv-audit'
  scope: kv
  properties: {
    workspaceId: logAnalyticsId
    logs: [{ categoryGroup: 'audit', enabled: true }]
  }
}

output name string = kv.name
output appInsightsSecretUri string = '${kv.properties.vaultUri}secrets/appinsights-connection-string'
output oboClientSecretUri string = '${kv.properties.vaultUri}secrets/entra-obo-client-secret'
output integrationClientSecretUri string = '${kv.properties.vaultUri}secrets/integration-client-secret'
output salesforceClientSecretUri string = '${kv.properties.vaultUri}secrets/salesforce-client-secret'
