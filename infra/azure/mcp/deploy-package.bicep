targetScope = 'resourceGroup'

@description('Existing Flex Consumption Function App name.')
param functionAppName string

@description('Region of the Function App.')
param location string = 'westus2'

@description('Private immutable ready-to-run release package URI. The Function managed identity must be able to read it.')
param packageUri string

resource functionApp 'Microsoft.Web/sites@2024-04-01' existing = {
  name: functionAppName
}

resource oneDeploy 'Microsoft.Web/sites/extensions@2022-09-01' = {
  parent: functionApp
  name: 'onedeploy'
  location: location
  properties: {
    packageUri: packageUri
    remoteBuild: false
  }
}
