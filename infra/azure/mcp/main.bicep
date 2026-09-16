targetScope = 'resourceGroup'

@description('Region for the Long Haul MCP control plane.')
param location string = 'westus2'

@description('Dedicated Long Haul MCP server Entra application/client ID.')
param mcpServerClientId string

@description('Tenant ID used to validate Long Haul MCP access tokens.')
param tenantId string

@description('Globally unique suffix used for Function/storage names.')
param nameSuffix string

@description('Principal ID of the existing GitHub OIDC deployment identity; receives blob data access only on MCP storage.')
param deploymentPrincipalId string

var functionName = 'longhaul-mcp-${nameSuffix}'
var storageName = 'lhmcp${replace(nameSuffix, '-', '')}'
var deploymentContainerName = 'app-package'
var releaseContainerName = 'release-artifacts'
var storageBlobDataContributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
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
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: deploymentContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource releaseContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: releaseContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource appServicePlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${functionName}-plan'
  location: location
  tags: tags
  kind: 'functionapp'
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
    publicNetworkAccess: 'Enabled'
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        {
          name: 'AzureWebJobsStorage__accountName'
          value: storage.name
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
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
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 10
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.11'
      }
    }
  }
  dependsOn: [
    deploymentContainer
  ]
}

resource functionStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, functionApp.id, storageBlobDataContributorRoleId)
  scope: storage
  properties: {
    roleDefinitionId: storageBlobDataContributorRoleId
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource deploymentStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, deploymentPrincipalId, storageBlobDataContributorRoleId)
  scope: storage
  properties: {
    roleDefinitionId: storageBlobDataContributorRoleId
    principalId: deploymentPrincipalId
    principalType: 'ServicePrincipal'
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
output storageName string = storage.name
output deploymentContainerUri string = '${storage.properties.primaryEndpoints.blob}${deploymentContainerName}'
output releaseContainerUri string = '${storage.properties.primaryEndpoints.blob}${releaseContainerName}'
output mcpEndpoint string = 'https://${functionApp.properties.defaultHostName}/runtime/webhooks/mcp'
