targetScope = 'resourceGroup'

@description('Azure region for the CPU-only reference vessel.')
param location string = 'eastus'

@description('The dedicated resource group expected by the protected workflow.')
param expectedResourceGroupName string

@allowed([
  'Standard_B1ms'
])
@description('The only approved reference SKU. GPU and automatic upscale are prohibited.')
param vmSize string = 'Standard_B1ms'

@minValue(30)
@maxValue(32)
@description('Bounded Standard HDD OS disk in GiB.')
param osDiskSizeGb int = 32

@description('Non-secret local account name. No inbound management path is created.')
param adminUsername string = 'longhaul'

@description('Public, non-secret bootstrap configuration written into cloud-init.')
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
output referenceVmName string = referenceVm.outputs.vmName
