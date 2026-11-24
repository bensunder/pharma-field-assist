using './main.bicep'

param environmentName = 'qa'
param imageTag = readEnvironmentVariable('IMAGE_TAG', 'local')
param acrName = 'acrcontosopfa'
param openAiAccountName = 'aoai-contoso-pfa-qa'
param openAiDeployment = 'gpt-4o'
param entraTenantId = readEnvironmentVariable('ENTRA_TENANT_ID')
param entraApiClientId = readEnvironmentVariable('ENTRA_API_CLIENT_ID')
param entraAudience = 'api://pharma-field-assist-qa'
param oncallEmail = 'pfa-dev@contoso-pharma.com'
param integrationClientId = readEnvironmentVariable('INTEGRATION_CLIENT_ID')
param integrationMode = 'live'
param salesforceLoginUrl = readEnvironmentVariable('SF_LOGIN_URL')
param salesforceClientId = readEnvironmentVariable('SF_CLIENT_ID')
param fabricSqlEndpoint = readEnvironmentVariable('FABRIC_SQL_ENDPOINT')
param fabricDatabase = readEnvironmentVariable('FABRIC_DATABASE')
