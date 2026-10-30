"""Deploy AWS hosting and install the origin/publisher on an existing SSH host."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import uuid

from publisher import atomic_json, render
from settings import load
import server


def behavior(origin_id, ttl=0, headers=None):
    headers = headers or []
    return {'TargetOriginId': origin_id, 'ViewerProtocolPolicy': 'https-only',
            'AllowedMethods': {'Quantity': 2, 'Items': ['GET', 'HEAD'], 'CachedMethods': {'Quantity': 2, 'Items': ['GET', 'HEAD']}},
            'ForwardedValues': {'QueryString': False, 'Cookies': {'Forward': 'none'}, 'Headers': {'Quantity': len(headers), **({'Items': headers} if headers else {})}},
            'MinTTL': 0, 'DefaultTTL': ttl, 'MaxTTL': ttl, 'Compress': False,
            'TrustedSigners': {'Enabled': False, 'Quantity': 0}}


def distribution(cfg, reference, origin, cache_behavior):
    return {'CallerReference': reference, 'Comment': cfg['project'] + ': ' + reference,
            'Origins': {'Quantity': 1, 'Items': [origin]}, 'DefaultCacheBehavior': cache_behavior,
            'Enabled': True, 'ViewerCertificate': {'CloudFrontDefaultCertificate': True},
            'PriceClass': cfg['aws']['price_class'], 'HttpVersion': 'http2', 'IsIPV6Enabled': False}


def run_server(cfg, script):
    # Authentication and a separately verified host key belong in the operator's SSH config.
    subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                    '-o', 'ConnectTimeout=15', cfg['server']['ssh_target'], 'sudo bash -s'],
                   input=script, text=True, encoding='utf-8', check=True, timeout=900)


def hosting(cfg, state, save, s3, cf):
    region, account = cfg['aws']['region'], cfg['aws']['account_id']
    bucket = state['bucket']
    if not state.get('bucketCreated'):
        args = {'Bucket': bucket, 'ObjectOwnership': 'BucketOwnerEnforced'}
        if region != 'us-east-1':
            args['CreateBucketConfiguration'] = {'LocationConstraint': region}
        try:
            s3.create_bucket(**args)
        except s3.exceptions.BucketAlreadyOwnedByYou:
            pass  # Resume after a successful create whose local journal write was interrupted.
        state['bucketCreated'] = True
        save()
    s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={k: True for k in ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')})
    s3.put_bucket_encryption(Bucket=bucket, ServerSideEncryptionConfiguration={'Rules': [{'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]})
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={'Status': 'Enabled'})
    s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={'Rules': [{'ID': 'replaced-config-seven-days', 'Status': 'Enabled', 'Filter': {'Prefix': ''}, 'NoncurrentVersionExpiration': {'NoncurrentDays': 7}}]})
    s3.put_bucket_tagging(Bucket=bucket, Tagging={'TagSet': [{'Key': 'Project', 'Value': cfg['project']}]})
    if not state.get('oac'):
        state['oac'] = cf.create_origin_access_control(OriginAccessControlConfig={'Name': bucket, 'Description': 'Private configuration objects', 'SigningProtocol': 'sigv4', 'SigningBehavior': 'always', 'OriginAccessControlOriginType': 's3'})['OriginAccessControl']['Id']
        save()
    if not state.get('configDistribution'):
        origin = {'Id': 'config-s3', 'DomainName': f'{bucket}.s3.{region}.amazonaws.com', 'S3OriginConfig': {'OriginAccessIdentity': ''}, 'OriginAccessControlId': state['oac']}
        d = cf.create_distribution(DistributionConfig=distribution(cfg, bucket + '-config', origin, behavior('config-s3', 300)))['Distribution']
        state['configDistribution'], state['configDomain'] = d['Id'], d['DomainName']
        save()
    policy = {'Version': '2012-10-17', 'Statement': [
        {'Sid': 'OnlyDesignatedCloudFront', 'Effect': 'Allow', 'Principal': {'Service': 'cloudfront.amazonaws.com'}, 'Action': 's3:GetObject', 'Resource': f"arn:aws:s3:::{bucket}/{state['prefix']}/*", 'Condition': {'StringEquals': {'AWS:SourceArn': f"arn:aws:cloudfront::{account}:distribution/{state['configDistribution']}"}}},
        {'Sid': 'RequireTLS', 'Effect': 'Deny', 'Principal': '*', 'Action': 's3:*', 'Resource': [f'arn:aws:s3:::{bucket}', f'arn:aws:s3:::{bucket}/*'], 'Condition': {'Bool': {'aws:SecureTransport': 'false'}}}]}
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
    print('Config distribution:', state['configDistribution'])


def origin(cfg, state, save, cf):
    run_server(cfg, server.origin(cfg, state))
    if not state.get('proxyDistribution'):
        s = cfg['server']
        source = {'Id': 'xray-origin', 'DomainName': s['origin_hostname'],
                  'CustomHeaders': {'Quantity': 1, 'Items': [{'HeaderName': 'X-Origin-Token', 'HeaderValue': state['originSecret']}]},
                  'CustomOriginConfig': {'HTTPPort': 80, 'HTTPSPort': s['https_port'], 'OriginProtocolPolicy': 'https-only', 'OriginSslProtocols': {'Quantity': 1, 'Items': ['TLSv1.2']}, 'OriginReadTimeout': 60}}
        headers = ['Sec-WebSocket-Key', 'Sec-WebSocket-Version', 'Sec-WebSocket-Protocol', 'Sec-WebSocket-Extensions']
        d = cf.create_distribution(DistributionConfig=distribution(cfg, state['bucket'] + '-proxy', source, behavior('xray-origin', 0, headers)))['Distribution']
        state['proxyDistribution'], state['proxyDomain'] = d['Id'], d['DomainName']
        save()
    state['originReady'] = True
    save()
    print('Proxy distribution:', state['proxyDistribution'])


def publish(cfg, base, state, save, root, iam, cf):
    if not state.get('originReady') or not state.get('configDistribution'):
        raise ValueError('Run hosting and origin before publish')
    for key in ('configDistribution', 'proxyDistribution'):
        d = cf.get_distribution(Id=state[key])['Distribution']
        if d['Status'] != 'Deployed' or not d['DistributionConfig']['Enabled']:
            raise ValueError('CloudFront is not ready. Wait until both distributions are Deployed, then rerun publish.')
    user = state['bucket'] + '-publisher'
    if not state.get('publisherUser'):
        iam.create_user(UserName=user, Tags=[{'Key': 'Project', 'Value': cfg['project']}])
        state['publisherUser'] = user
        save()
    resources = [f"arn:aws:s3:::{state['bucket']}/{state['prefix']}/{name}" for name in ('clash.yaml', 'nodes.yaml')]
    iam.put_user_policy(UserName=user, PolicyName='PublishTwoObjects', PolicyDocument=json.dumps({'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Action': 's3:PutObject', 'Resource': resources}]}))
    credential_file = root / 'publisher-credentials'
    if not state.get('publisherAccessKeyId'):
        key = iam.create_access_key(UserName=user)['AccessKey']
        credential_file.write_text(f"[default]\naws_access_key_id={key['AccessKeyId']}\naws_secret_access_key={key['SecretAccessKey']}\n", encoding='utf-8')
        credential_file.chmod(0o600)
        state['publisherAccessKeyId'] = key['AccessKeyId']
        save()
    if not credential_file.exists() and not state.get('publisherInstalled'):
        raise ValueError('Publisher key is missing. Recover or revoke the recorded key before creating another.')
    credentials = credential_file.read_text(encoding='utf-8') if credential_file.exists() else None
    run_server(cfg, server.publisher(cfg, base, state, credentials))
    state['publisherInstalled'] = True
    save()
    credential_file.unlink(missing_ok=True)
    print('Subscription URL (secret):', f"https://{state['configDomain']}/{state['prefix']}/clash.yaml")


def preflight(cfg, base):
    # Exercise rendering without contacting AWS or exposing any real deployment state.
    sample = {'uuid': '00000000-0000-4000-8000-000000000000', 'proxyDomain': 'proxy.example.com',
              'configDomain': 'config.example.com', 'wsPath': '/ws/example', 'prefix': 'example',
              'discovery': cfg['discovery'], 'clash': cfg['clash']}
    import copy
    sample = copy.deepcopy(sample)
    sample['discovery']['excluded_ips'] = []
    render(base, sample, [{'ip': '1.1.1.1', 'pop': cfg['discovery']['pop_prefixes'][0] + '1-P1'}])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'bootstrap', 'hosting', 'origin', 'publish', 'status', 'verify'])
    parser.add_argument('--config', type=Path, default=Path('config/deployment.json'))
    parser.add_argument('--state-dir', type=Path, default=Path('.state'))
    args = parser.parse_args()
    cfg, base = load(args.config)
    preflight(cfg, base)
    if args.action == 'check':
        print('Configuration and Clash rendering passed. No AWS or SSH calls made.')
        return
    if os.name != 'posix':
        raise RuntimeError('Run deployment from Linux, WSL, or AWS CloudShell; local checks work on Windows')
    os.umask(0o077)
    root = args.state_dir.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    import fcntl
    # Serialize the entire deployment, including state reads and IAM key creation.
    lock = (root / 'deploy.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    import boto3
    if boto3.client('sts').get_caller_identity()['Account'] != cfg['aws']['account_id']:
        raise ValueError('AWS credentials belong to a different account')
    statefile = root / 'state.json'
    state = json.loads(statefile.read_text(encoding='utf-8')) if statefile.exists() else {}
    identity = {'project': cfg['project'], 'aws': cfg['aws'], 'server': cfg['server']}
    if state and state['deployment'] != identity:
        raise ValueError('This state belongs to different infrastructure settings. Use a separate state directory for a new deployment.')
    if not state:
        if args.action in ('status', 'verify'):
            raise ValueError('No deployment state found')
        state = {'deployment': identity, 'bucket': cfg['project'] + '-' + secrets.token_hex(8),
                 'prefix': secrets.token_hex(32), 'uuid': str(uuid.uuid4()),
                 'wsPath': '/ws/' + secrets.token_hex(24), 'originSecret': secrets.token_hex(32)}
        atomic_json(statefile, state)
    save = lambda: atomic_json(statefile, state)
    s3 = boto3.client('s3', region_name=cfg['aws']['region'])
    cf = boto3.client('cloudfront')
    if args.action == 'bootstrap':
        run_server(cfg, server.bootstrap(cfg))
    elif args.action == 'hosting':
        hosting(cfg, state, save, s3, cf)
    elif args.action == 'origin':
        origin(cfg, state, save, cf)
    elif args.action == 'publish':
        publish(cfg, base, state, save, root, boto3.client('iam'), cf)
    else:
        for key in ('configDistribution', 'proxyDistribution'):
            if state.get(key):
                d = cf.get_distribution(Id=state[key])['Distribution']
                print(key, d['Id'], d['Status'], d['DistributionConfig']['Enabled'])
                if args.action == 'verify' and (d['Status'] != 'Deployed' or not d['DistributionConfig']['Enabled']):
                    raise RuntimeError('Distribution is not ready')
            elif args.action == 'verify':
                raise RuntimeError('Incomplete deployment: ' + key)
        if args.action == 'verify':
            if not all(s3.get_public_access_block(Bucket=state['bucket'])['PublicAccessBlockConfiguration'].values()):
                raise RuntimeError('S3 public access block is incomplete')
            if s3.get_bucket_versioning(Bucket=state['bucket'])['Status'] != 'Enabled':
                raise RuntimeError('S3 versioning is disabled')
            project = cfg['project']
            run_server(cfg, f'set -eu\nsystemctl is-active nginx {project}.service {project}-publish.timer\ncat /opt/{project}/status.json\ncertbot renew --cert-name {project} --dry-run --no-random-sleep-on-renew --run-deploy-hooks\n')


if __name__ == '__main__':
    main()
