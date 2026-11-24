// PharmaFieldAssist — Azure Container Apps platform (one deployment per environment: dev | qa | prod)
//
//   UAMI ──(AcrPull)──────────────► ACR
//     │ ──(Key Vault Secrets User)─► Key Vault  (secrets referenced by ACA, never in pipeline vars)
//     │ ──(Cognitive Services OpenAI User)► Azure OpenAI
//   ACA Environment (VNet-integrable) ─► Log Analytics ◄─ App Insights ◄─ alerts → Action Group
//     ├─ api  (FastAPI)  multiple-revision mode, internal-only ingress in prod (front door / web only)
//     └─ web  (nginx + React)
targetScope = 'resourceGroup'

@allowed(['dev', 'qa', 'prod'])
param environmentName string
param location string = resourceGroup().location
param appName string = 'pfa'
@description('Image tag built by the pipeline (git SHA). Never "latest".')
param imageTag string
param acrName string
@description('Existing Azure OpenAI account name (same subscription).')
param openAiAccountName string
param openAiDeployment string = 'gpt-4o'
param entraTenantId string
param entraApiClientId string
param entraAudience string
@description('App registration for app-only calls (Fabric SQL endpoint).')
param integrationClientId string
@allowed(['live', 'mock'])
param integrationMode string = 'live'
param salesforceLoginUrl string
param salesforceClientId string
param fabricSqlEndpoint string
param fabricDatabase string
@description('Email for on-call alert routing (Sev1/Sev2).')
param oncallEmail string
param minReplicas int = environmentName == 'prod' ? 2 : 1
param maxReplicas int = environmentName == 'prod' ? 10 : 3

var prefix = '${appName}-${environmentName}'
var tags = {
  app: appName
  env: environmentName
  'data-classification': 'confidential'
  'managed-by': 'bicep'
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    prefix: prefix
    location: location
    tags: tags
    oncallEmail: oncallEmail
    retentionDays: environmentName == 'prod' ? 365 : 30
  }
}

module identity 'modules/identity.bicep' = {
  name: 'identity'
  params: {
    prefix: prefix
    location: location
    tags: tags
    acrName: acrName
    openAiAccountName: openAiAccountName
  }
}

module keyVault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: {
    prefix: prefix
    location: location
    tags: tags
    readerPrincipalId: identity.outputs.principalId
    logAnalyticsId: monitoring.outputs.logAnalyticsId
    purgeProtection: environmentName == 'prod'
  }
}

module apps 'modules/containerapps.bicep' = {
  name: 'containerapps'
  params: {
    prefix: prefix
    location: location
    tags: tags
    environmentName: environmentName
    logAnalyticsCustomerId: monitoring.outputs.logAnalyticsCustomerId
    logAnalyticsName: monitoring.outputs.logAnalyticsName
    appInsightsConnectionStringSecretUri: keyVault.outputs.appInsightsSecretUri
    oboClientSecretUri: keyVault.outputs.oboClientSecretUri
    integrationClientSecretUri: keyVault.outputs.integrationClientSecretUri
    salesforceClientSecretUri: keyVault.outputs.salesforceClientSecretUri
    integrationClientId: integrationClientId
    integrationMode: integrationMode
    salesforceLoginUrl: salesforceLoginUrl
    salesforceClientId: salesforceClientId
    fabricSqlEndpoint: fabricSqlEndpoint
    fabricDatabase: fabricDatabase
    identityId: identity.outputs.identityId
    identityClientId: identity.outputs.clientId
    acrLoginServer: '${acrName}.azurecr.io'
    imageTag: imageTag
    openAiEndpoint: 'https://${openAiAccountName}.openai.azure.com'
    openAiDeployment: openAiDeployment
    entraTenantId: entraTenantId
    entraApiClientId: entraApiClientId
    entraAudience: entraAudience
    minReplicas: minReplicas
    maxReplicas: maxReplicas
  }
}

module alerts 'modules/alerts.bicep' = {
  name: 'alerts'
  params: {
    prefix: prefix
    location: location
    tags: tags
    appInsightsId: monitoring.outputs.appInsightsId
    logAnalyticsId: monitoring.outputs.logAnalyticsId
    actionGroupId: monitoring.outputs.actionGroupId
    apiAppId: apps.outputs.apiAppId
  }
}

output apiFqdn string = apps.outputs.apiFqdn
output webFqdn string = apps.outputs.webFqdn
output apiAppName string = apps.outputs.apiAppName
output keyVaultName string = keyVault.outputs.name
