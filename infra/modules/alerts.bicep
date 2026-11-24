// Alerts map directly to the incident severities in docs/RUNBOOK.md.
param prefix string
param location string
param tags object
param appInsightsId string
param logAnalyticsId string
param actionGroupId string
param apiAppId string

// Sev 1 — API 5xx rate > 5% over 5 min (field reps broadly impacted)
resource http5xx 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'alert-${prefix}-api-5xx-rate'
  location: location
  tags: tags
  properties: {
    displayName: '[Sev1] PFA API 5xx rate > 5%'
    severity: 1
    enabled: true
    scopes: [appInsightsId]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    criteria: {
      allOf: [
        {
          query: '''
requests
| summarize total = count(), failed = countif(toint(resultCode) >= 500)
| extend rate = todouble(failed) / iff(total == 0, 1.0, todouble(total))
| where total >= 20 and rate > 0.05
'''
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    actions: { actionGroups: [actionGroupId] }
  }
}

// Sev 2 — a downstream circuit breaker opened (Graph / Veeva / Fabric degraded)
resource breakerOpen 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'alert-${prefix}-breaker-open'
  location: location
  tags: tags
  properties: {
    displayName: '[Sev2] PFA dependency circuit breaker opened'
    severity: 2
    enabled: true
    scopes: [logAnalyticsId]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    criteria: {
      allOf: [
        {
          query: '''
ContainerAppConsoleLogs_CL
| where Log_s has "breaker_opened"
| extend p = parse_json(Log_s)
| summarize opens = count() by dependency = tostring(p.dependency)
'''
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          dimensions: [{ name: 'dependency', operator: 'Include', values: ['*'] }]
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    actions: { actionGroups: [actionGroupId] }
  }
}

// Sev 3 — p95 chat latency > 8s over 15 min
resource latency 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = {
  name: 'alert-${prefix}-chat-p95'
  location: location
  tags: tags
  properties: {
    displayName: '[Sev3] PFA /api/chat p95 latency > 8s'
    severity: 3
    enabled: true
    scopes: [appInsightsId]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      allOf: [
        {
          query: '''
requests
| where name has "/api/chat"
| summarize p95 = percentile(duration, 95)
| where p95 > 8000
'''
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    actions: { actionGroups: [actionGroupId] }
  }
}

// Sev 2 — replica restarts (crash loop) on the API
resource restarts 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-${prefix}-api-restarts'
  location: 'global'
  tags: tags
  properties: {
    description: '[Sev2] PFA API replica restarts'
    severity: 2
    enabled: true
    scopes: [apiAppId]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'restarts'
          criterionType: 'StaticThresholdCriterion'
          metricName: 'RestartCount'
          metricNamespace: 'Microsoft.App/containerApps'
          operator: 'GreaterThan'
          threshold: 2
          timeAggregation: 'Total'
        }
      ]
    }
    actions: [{ actionGroupId: actionGroupId }]
  }
}
