This is a Python project for syncing events from the Swarm contracts (various deployments) and storing them in a JSONLines cache.

Follow the approach used in ~/repo/swarm-volume-registry/services/indexer. Make a note of the data model. 

We aren't making a projector, so no testing against chain scenarios is needed. Successful, resumable execution of the acquisition layer is the material part. 

As I understand it, the only decoding that needs to happen is standard Web3 codec stuff plus adding names to event fields and possibly struct fields. There are no enums in the Swarm contracts. The package will need event schema for events across different contracts and versions, including those with matching names that need the specific contract address to disambiguate.


The main entry point should be a commandline tool that syncs the cache for given contracts or events, starting from the deployment block or from the stored checkpoint.

When imported as a package, it exposes functionality that loads the local cache as an array of objects.

