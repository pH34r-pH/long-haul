using '../main.bicep'

param location = 'eastus'
param expectedResourceGroupName = 'replace-with-dedicated-long-haul-resource-group'
param vmSize = 'Standard_B1ms'
param osDiskSizeGb = 32
param adminUsername = 'longhaul'
param bootstrap = {
  longHaulRevision: 'cab1293e6e8012cabb8b8e3450efa9b146134875'
  llamaCppRevision: 'b78a39a2f93b13a79a3e01aff3f14274efb43afc'
  modelUrl: 'https://huggingface.co/bartowski/SmolLM2-135M-Instruct-GGUF/resolve/main/SmolLM2-135M-Instruct-Q4_K_M.gguf'
  modelSha256: '2e8040ceae7815abe0dcb3540b9995eaa1fa0d2ca9e797d0a635ae4433c68c2d'
}
