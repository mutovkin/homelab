import * as eq12 from "./machines/eq12";
import * as n5pro from "./machines/n5pro";

// Export all resources for Pulumi state tracking
export const eq12Resources = {
  homeAssistant: eq12.homeAssistant,
  debDocker: eq12.debDocker,
  ubuntuDocker: eq12.ubuntuDocker,
  nginxProxyManager: eq12.nginxProxyManager,
};

export const n5proResources = {
  truenas: n5pro.truenas,
  dockerHost: n5pro.dockerHost,
};
