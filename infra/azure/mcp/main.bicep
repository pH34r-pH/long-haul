targetScope = 'resourceGroup'

@description('Region for the Long Haul MCP control plane.')
param location string = 'westus2'

@description('Dedicated Long Haul MCP server Entra application/client ID.')
param mcpServerClientId string

@description('Tenant ID used to validate Long Haul MCP access tokens.')
param tenantId string

@description('Globally unique suffix used for Function/storage names.')
param nameSuffix string

var functionName = 'longhaul-mcp-${nameSuffix}'
var storageName = 'lhmcp${replace(nameSuffix, '-', '')}'
var tags = {
  project: 'long-haul'
  purpose: 'mcp-control-plane'
  environment: 'reference'
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
  }
}

resource appServicePlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${functionName}-plan'
  location: location
  tags: tags
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        {
          name: 'AzureWebJobsStorage'
          value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storage.listKeys().keys[0].value}'
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'python'
        }
        {
          name: 'LONG_HAUL_AUTH_PROVIDER'
          value: 'entra'
        }
        {
          name: 'LONG_HAUL_MCP_SERVER_CLIENT_ID'
          value: mcpServerClientId
        }
      ]
    }
  }
}

resource authConfig 'Microsoft.Web/sites/config@2024-04-01' = {
  parent: functionApp
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'Return401'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: mcpServerClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            'api://${mcpServerClientId}'
            mcpServerClientId
          ]
          defaultAuthorizationPolicy: {
            allowedApplications: []
          }
        }
      }
    }
    login: {
      tokenStore: {
        enabled: true
      }
    }
    httpSettings: {
      requireHttps: true
    }
  }
}

output functionName string = functionApp.name
output functionHostname string = functionApp.properties.defaultHostName
output functionPrincipalId string = functionApp.identity.principalId
output mcpEndpoint string = 'https://${functionApp.properties.defaultHostName}/runtime/webhooks/mcp'
