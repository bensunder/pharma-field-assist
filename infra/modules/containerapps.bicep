param prefix string
param location string
param tags object
param environmentName string
param logAnalyticsCustomerId string
param logAnalyticsName string
param appInsightsConnectionStringSecretUri string
param oboClientSecretUri string
param integrationClientSecretUri string
param salesforceClientSecretUri string
param integrationClientId string
@allowed(['live', 'mock'])
param integrationMode string
param salesforceLoginUrl string
param salesforceClientId string
param fabricSqlEndpoint string
param fabricDatabase string
param identityId string
param identityClientId string
param acrLoginServer string
param imageTag string
param openAiEndpoint string
param openAiDeployment string
param entraTenantId string
param entraApiClientId string
param entraAudience string
param minReplicas int
param maxReplicas int

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${prefix}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
    zoneRedundant: false // true in prod requires a VNet-integrated environment
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
  }
}

var identity = {
  type: 'UserAssigned'
  userAssignedIdentities: { '${identityId}': {} }
}

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-${prefix}-api'
  location: location
  tags: tags
  identity: identity
  properties: {
    managedEnvironmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      // Multiple revision mode: the pipeline deploys a new revision at 0% traffic, smoke-tests it via
      // its revision label URL, then shifts traffic. Rollback = point traffic back at the previous label.
      activeRevisionsMode: 'Multiple'
      maxInactiveRevisions: 5
      ingress: {
        external: environmentName != 'prod' // prod API is reachable only through the web app / gateway
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
      }
      registries: [{ server: acrLoginServer, identity: identityId }]
      secrets: [
        { name: 'appinsights-cs', keyVaultUrl: appInsightsConnectionStringSecretUri, identity: identityId }
        { name: 'obo-client-secret', keyVaultUrl: oboClientSecretUri, identity: identityId }
        { name: 'integration-client-secret', keyVaultUrl: integrationClientSecretUri, identity: identityId }
        { name: 'salesforce-client-secret', keyVaultUrl: salesforceClientSecretUri, identity: identityId }
      ]
    }
    template: {
      revisionSuffix: take(replace(imageTag, '.', '-'), 10)
      containers: [
        {
          name: 'api'
          image: '${acrLoginServer}/pfa-api:${imageTag}'
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'PFA_ENVIRONMENT', value: environmentName }
            { name: 'PFA_APP_VERSION', value: imageTag }
            { name: 'PFA_AUTH_MODE', value: 'entra' }
            { name: 'PFA_ENTRA_TENANT_ID', value: entraTenantId }
            { name: 'PFA_ENTRA_API_CLIENT_ID', value: entraApiClientId }
            { name: 'PFA_ENTRA_AUDIENCE', value: entraAudience }
            { name: 'PFA_ENTRA_API_CLIENT_SECRET', secretRef: 'obo-client-secret' }
            { name: 'PFA_AZURE_OPENAI_ENDPOINT', value: openAiEndpoint }
            { name: 'PFA_AZURE_OPENAI_DEPLOYMENT', value: openAiDeployment }
            { name: 'AZURE_CLIENT_ID', value: identityClientId } // DefaultAzureCredential -> this UAMI
            { name: 'PFA_APPLICATIONINSIGHTS_CONNECTION_STRING', secretRef: 'appinsights-cs' }
            { name: 'PFA_ENABLE_FAULT_INJECTION', value: environmentName == 'prod' ? 'false' : 'true' }
            // Live integrations (Graph via OBO in entra mode, Salesforce, Fabric SQL endpoint)
            { name: 'PFA_GRAPH_MODE', value: integrationMode }
            { name: 'PFA_CRM_MODE', value: integrationMode }
            { name: 'PFA_FABRIC_MODE', value: integrationMode }
            { name: 'PFA_INTEGRATION_CLIENT_ID', value: integrationClientId }
            { name: 'PFA_INTEGRATION_CLIENT_SECRET', secretRef: 'integration-client-secret' }
            { name: 'PFA_SF_LOGIN_URL', value: salesforceLoginUrl }
            { name: 'PFA_SF_CLIENT_ID', value: salesforceClientId }
            { name: 'PFA_SF_CLIENT_SECRET', secretRef: 'salesforce-client-secret' }
            { name: 'PFA_FABRIC_SQL_ENDPOINT', value: fabricSqlEndpoint }
            { name: 'PFA_FABRIC_DATABASE', value: fabricDatabase }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/health/live', port: 8000 }
              periodSeconds: 15
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: { path: '/health/ready', port: 8000 }
              periodSeconds: 10
              failureThreshold: 3
            }
            {
              type: 'Startup'
              httpGet: { path: '/health/live', port: 8000 }
              periodSeconds: 5
              failureThreshold: 12
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [{ name: 'http', http: { metadata: { concurrentRequests: '40' } } }]
      }
    }
  }
}

resource web 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-${prefix}-web'
  location: location
  tags: tags
  identity: identity
  properties: {
    managedEnvironmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: true, targetPort: 8080, transport: 'http', allowInsecure: false }
      registries: [{ server: acrLoginServer, identity: identityId }]
    }
    template: {
      containers: [
        {
          name: 'web'
          image: '${acrLoginServer}/pfa-web:${imageTag}'
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [{ name: 'API_UPSTREAM', value: 'http://${api.name}' }] // internal env DNS
          probes: [{ type: 'Liveness', httpGet: { path: '/', port: 8080 }, periodSeconds: 30 }]
        }
      ]
      scale: { minReplicas: minReplicas, maxReplicas: 4 }
    }
  }
}

output apiAppId string = api.id
output apiAppName string = api.name
output apiFqdn string = api.properties.configuration.ingress.fqdn
output webFqdn string = web.properties.configuration.ingress.fqdn
