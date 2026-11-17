// The API compatibility contract is the same JSON file shipped by the importer.
const API_CONTRACT_URL = "api-contract.json";
async function loadApiContract() {
  const response = await fetch(chrome.runtime.getURL(API_CONTRACT_URL));
  if (!response.ok) throw new Error("The extension API contract is missing.");
  return response.json();
}
