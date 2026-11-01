import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';

import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';
import { Construct } from 'constructs';

class PythonLocalBundling implements cdk.ILocalBundling {
  constructor(private readonly sourceDir: string) {}

  public tryBundle(outputDir: string): boolean {
    execFileSync(
      'python3',
      [
        '-m',
        'pip',
        'install',
        '--disable-pip-version-check',
        '--no-compile',
        '--requirement',
        path.join(this.sourceDir, 'requirements.txt'),
        '--target',
        outputDir,
      ],
      { stdio: 'inherit' },
    );
    fs.copyFileSync(
      path.join(this.sourceDir, 'tenmo_auto_booking.py'),
      path.join(outputDir, 'tenmo_auto_booking.py'),
    );

    // pip on macOS installs an incompatible optional speedups binary. The
    // websockets package automatically falls back to its pure-Python version.
    this.removeNativeArtifacts(outputDir);
    return true;
  }

  private removeNativeArtifacts(directory: string): void {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const entryPath = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        if (entry.name === '__pycache__') {
          fs.rmSync(entryPath, { recursive: true, force: true });
        } else {
          this.removeNativeArtifacts(entryPath);
        }
      } else if (entry.name.endsWith('.so') || entry.name.endsWith('.dylib')) {
        fs.rmSync(entryPath);
      }
    }
  }
}

export interface BackendStackProps extends cdk.StackProps {
  /** Deployment name, for example dev, staging, or prod. */
  readonly environmentName?: string;
  /** Directory containing tenmo_auto_booking.py. Defaults to the repo root. */
  readonly lambdaCodePath?: string;
  /** Tanmo credentials written to the Lambda environment. */
  readonly tanmoUsername?: string;
  readonly tanmoPassword?: string;
  /** Default Tanmo WebSocket device id. Defaults to 0. */
  readonly tanmoDeviceId?: number;
  /** Allowed browser origins. Defaults to '*'. */
  readonly corsOrigins?: string[];
  /** Require x-api-key on POST /booking. Defaults to true. */
  readonly apiKeyRequired?: boolean;
  /** Route 53 zone details; all three DNS properties are required together. */
  readonly hostedZoneId?: string;
  readonly hostedZoneName?: string;
  readonly apiDomainName?: string;
}

/** Deploys the Tanmo booking Lambda behind API Gateway REST API. */
export class BackendStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: BackendStackProps = {}) {
    super(scope, id, props);

    const environmentName = props.environmentName ?? 'dev';
    const lambdaCodePath =
      props.lambdaCodePath ?? path.resolve(__dirname, '../..');
    const corsOrigins = props.corsOrigins?.length ? props.corsOrigins : ['*'];
    const apiKeyRequired = props.apiKeyRequired ?? true;
    const tanmoUsername =
      props.tanmoUsername ?? process.env.TANMO_USERNAME ?? 'odestudio';
    const tanmoPassword = props.tanmoPassword ?? process.env.TANMO_PASSWORD;
    const tanmoDeviceId =
      props.tanmoDeviceId ?? Number(process.env.TANMO_DEVICE_ID ?? '0');

    if (!tanmoPassword) {
      throw new Error(
        'TANMO_PASSWORD must be set when synthesizing or deploying this stack',
      );
    }
    if (!Number.isInteger(tanmoDeviceId) || tanmoDeviceId < 0) {
      throw new Error('TANMO_DEVICE_ID must be a non-negative integer');
    }

    const bookingFunction = new lambda.Function(this, 'BookingFunction', {
      functionName: `ode-studio-${environmentName}-tanmo-booking`,
      description: `ODE Studio ${environmentName} Tanmo booking API`,
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'tenmo_auto_booking.lambda_handler',
      code: lambda.Code.fromAsset(lambdaCodePath, {
        bundling: {
          image: lambda.Runtime.PYTHON_3_12.bundlingImage,
          local: new PythonLocalBundling(lambdaCodePath),
          command: [
            'bash',
            '-c',
            [
              'pip install --no-cache-dir -r /asset-input/requirements.txt -t /asset-output',
              'cp /asset-input/tenmo_auto_booking.py /asset-output/',
            ].join(' && '),
          ],
        },
      }),
      timeout: cdk.Duration.seconds(28),
      memorySize: 256,
      environment: {
        ENVIRONMENT: environmentName,
        TANMO_USERNAME: tanmoUsername,
        TANMO_PASSWORD: tanmoPassword,
        TANMO_DEVICE_ID: tanmoDeviceId.toString(),
        CORS_ALLOW_ORIGINS: corsOrigins.join(','),
      },
      tracing: lambda.Tracing.ACTIVE,
    });

    cdk.Tags.of(bookingFunction).add('Environment', environmentName);
    cdk.Tags.of(bookingFunction).add('Service', 'ode-studio-backend');

    const api = new apigateway.RestApi(this, 'BookingApi', {
      restApiName: `ode-studio-${environmentName}-booking-api`,
      description: 'ODE Studio Tanmo booking REST API',
      endpointTypes: [apigateway.EndpointType.REGIONAL],
      deployOptions: {
        stageName: environmentName,
        metricsEnabled: true,
        tracingEnabled: true,
      },
      defaultCorsPreflightOptions: {
        allowOrigins: corsOrigins,
        allowCredentials: !corsOrigins.includes('*'),
        allowHeaders: [
          'Authorization',
          'Content-Type',
          'X-Amz-Date',
          'X-Api-Key',
          'X-Amz-Security-Token',
        ],
        allowMethods: ['GET', 'POST', 'OPTIONS'],
      },
    });

    const booking = api.root.addResource('booking');
    booking.addMethod(
      'POST',
      new apigateway.LambdaIntegration(bookingFunction, { proxy: true }),
      { apiKeyRequired },
    );
    booking.addMethod(
      'GET',
      new apigateway.LambdaIntegration(bookingFunction, { proxy: true }),
      { apiKeyRequired },
    );

    if (apiKeyRequired) {
      const apiKey = api.addApiKey('BookingApiKey');
      const usagePlan = api.addUsagePlan('BookingUsagePlan', {
        name: `ode-studio-${environmentName}-booking`,
        throttle: { rateLimit: 2, burstLimit: 2 },
        quota: { limit: 100, period: apigateway.Period.DAY },
      });
      usagePlan.addApiKey(apiKey);
      usagePlan.addApiStage({ stage: api.deploymentStage });

      new cdk.CfnOutput(this, 'ApiKeyId', {
        value: apiKey.keyId,
        description: 'Use AWS CLI get-api-key --include-value to retrieve it',
      });
    }

    this.addCustomDomain(api, props);

    new cdk.CfnOutput(this, 'BookingApiEndpoint', {
      value: api.urlForPath('/booking'),
      description: 'POST endpoint for the Tanmo booking Lambda',
    });
  }

  private addCustomDomain(
    api: apigateway.RestApi,
    props: BackendStackProps,
  ): void {
    const dnsValues = [
      props.hostedZoneId,
      props.hostedZoneName,
      props.apiDomainName,
    ];
    const configuredDnsValues = dnsValues.filter(Boolean).length;

    if (configuredDnsValues === 0) {
      return;
    }
    if (configuredDnsValues !== dnsValues.length) {
      throw new Error(
        'hostedZoneId, hostedZoneName, and apiDomainName must be provided together',
      );
    }

    const zone = route53.HostedZone.fromHostedZoneAttributes(this, 'HostedZone', {
      hostedZoneId: props.hostedZoneId!,
      zoneName: props.hostedZoneName!,
    });
    const certificate = new acm.Certificate(this, 'ApiCertificate', {
      domainName: props.apiDomainName!,
      validation: acm.CertificateValidation.fromDns(zone),
    });
    const domain = new apigateway.DomainName(this, 'ApiDomain', {
      domainName: props.apiDomainName!,
      certificate,
      endpointType: apigateway.EndpointType.REGIONAL,
      securityPolicy: apigateway.SecurityPolicy.TLS_1_2,
    });
    domain.addBasePathMapping(api);

    new route53.ARecord(this, 'ApiAliasRecord', {
      zone,
      recordName: props.apiDomainName!,
      target: route53.RecordTarget.fromAlias(
        new targets.ApiGatewayDomain(domain),
      ),
    });

    new cdk.CfnOutput(this, 'ApiDomainName', {
      value: domain.domainName,
      description: 'Custom REST API domain',
    });
  }
}
