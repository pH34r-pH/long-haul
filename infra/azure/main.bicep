targetScope = 'resourceGroup'

@description('Azure region for the reference vessel. The protected workflow prices this exact region before deployment.')
param location string = 'eastus'

@description('Dedicated resource group expected by the protected deployment workflow.')
param expectedResourceGroupName string

@allowed([
  'Standard_B2as_v2'
  'Standard_B2s'
])
@description('Small CPU-only reference SKU. GPU and large SKUs are intentionally not accepted.')
param vmSize string = 'Standard_B2as_v2'

@minValue(32)
@maxValue(64)
@description('Bounded Standard SSD OS disk size in GiB.')
param osDiskSizeGb int = 64

@description('Non-secret local account name. No inbound SSH path is created by this stack.')
param adminUsername string = 'longhaul'

@description('Non-secret bootstrap configuration rendered into cloud-init.')
param bootstrap object

var namePrefix = 'longhaul-reference'

module network './modules/network.bicep' = {
  name: 'reference-network'
  params: {
    location: location
    namePrefix: namePrefix
  }
}

module referenceVm './modules/reference-vm.bicep' = {
  name: 'reference-vm'
  params: {
    location: location
    namePrefix: namePrefix
    subnetId: network.outputs.subnetId
    publicIpId: network.outputs.publicIpId
    vmSize: vmSize
    osDiskSizeGb: osDiskSizeGb
    adminUsername: adminUsername
    customData: loadTextContent('cloud-init/reference-vessel.yaml')
    bootstrap: bootstrap
  }
}

output expectedResourceGroupName string = expectedResourceGroupName
output deployedResourceGroupName string = resourceGroup().name
output referenceVmId string = referenceVm.outputs.vmId
output referenceVmName string = referenceVm.outputs.vmName
