targetScope = 'resourceGroup'

@description('Existing Flex Consumption Function App name.')
param functionAppName string

@description('Region of the Function App.')
param location string = 'westus2'

@description('Private immutable ready-to-run release package URI. The Function managed identity must be able to read it.')
param packageUri string

// Keep this as a top-level resource rather than a parented child. This is the
// shape documented by Azure Functions for Flex Consumption One Deploy and it
// also avoids the incomplete discriminated Bicep type for `onedeploy`.
resource oneDeploy 'Microsoft.Web/sites/extensions@2022-09-01' = {
  name: '${functionAppName}/onedeploy'
  location: location
  properties: {
    packageUri: packageUri
    remoteBuild: false
  }
}
