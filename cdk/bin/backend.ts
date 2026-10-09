#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';

import { BackendStack } from '../lib/backend';

const app = new cdk.App();

const contextString = (name: string): string | undefined => {
  const value = app.node.tryGetContext(name);
  return typeof value === 'string' && value.length > 0 ? value : undefined;
};

const environmentName =
  contextString('environment') ?? process.env.ENVIRONMENT ?? 'dev';
const corsOrigins = contextString('corsOrigins')
  ?.split(',')
  .map((origin) => origin.trim())
  .filter(Boolean);
const apiKeyRequiredContext = app.node.tryGetContext('apiKeyRequired');

new BackendStack(app, 'OdeStudioBackendStack', {
  stackName: `ode-studio-${environmentName}-backend`,
  environmentName,
  corsOrigins,
  apiKeyRequired:
    apiKeyRequiredContext === undefined
      ? true
      : String(apiKeyRequiredContext).toLowerCase() !== 'false',
  hostedZoneId: contextString('hostedZoneId'),
  hostedZoneName: contextString('hostedZoneName'),
  apiDomainName: contextString('apiDomainName'),
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
});
