import boto3
import csv
import sys
import os
import zipfile
import tempfile
import shutil
import subprocess
import json
from datetime import datetime
from collections import defaultdict
from pathlib import Path

def check_aws_credentials():
    """Verify AWS credentials are configured"""
    try:
        sts = boto3.client('sts')
        identity = sts.get_caller_identity()
        print(f"✓ AWS Credentials validated")
        print(f"  Account: {identity['Account']}")
        print(f"  User/Role: {identity['Arn']}")
        return True
    except Exception as e:
        print(f"✗ AWS Credentials Error: {e}")
        print("\nPlease configure AWS credentials using one of these methods:")
        print("1. Run: aws configure")
        print("2. Set environment variables:")
        print("   export AWS_ACCESS_KEY_ID=your_key")
        print("   export AWS_SECRET_ACCESS_KEY=your_secret")
        print("   export AWS_DEFAULT_REGION=us-east-1")
        print("3. Use AWS SSO: aws sso login --profile your-profile")
        return False

def check_compatibility_tools():
    """Check if compatibility checking tools are available"""
    tools = {
        'vermin': False,
        'pyupgrade': False,
        'pylint': False
    }
    
    print("\nChecking for compatibility analysis tools...")
    for tool in tools.keys():
        try:
            subprocess.run([tool, '--version'], capture_output=True, check=True, timeout=5)
            tools[tool] = True
            print(f"  ✓ {tool} found")
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            print(f"  ✗ {tool} not found (optional)")
    
    if not any(tools.values()):
        print("\n⚠️  No compatibility tools found. Install with:")
        print("  pip install vermin pyupgrade pylint")
        print("\nContinuing with basic compatibility checks...")
    
    return tools

def get_all_lambda_functions(region=None):
    """Retrieve all Lambda functions in specified region"""
    if region:
        lambda_client = boto3.client('lambda', region_name=region)
    else:
        lambda_client = boto3.client('lambda')
    
    functions = []
    try:
        paginator = lambda_client.get_paginator('list_functions')
        for page in paginator.paginate():
            functions.extend(page['Functions'])
    except Exception as e:
        print(f"Error retrieving functions: {e}")
        raise
    
    return functions

def check_cloudformation_managed(function_name, lambda_client):
    """Check if a Lambda function is managed by CloudFormation"""
    try:
        response = lambda_client.get_function(FunctionName=function_name)
        arn = response['Configuration']['FunctionArn']
        tags_response = lambda_client.list_tags(Resource=arn)
        tags = tags_response.get('Tags', {})
        
        if 'aws:cloudformation:stack-name' in tags or 'aws:cloudformation:logical-id' in tags:
            return True, tags.get('aws:cloudformation:stack-name', 'N/A')
    except Exception as e:
        print(f"  Warning: Could not check tags for {function_name}: {e}")
    
    return False, None

def download_lambda_code(function_name, lambda_client, temp_dir):
    """Download Lambda function code for analysis"""
    try:
        response = lambda_client.get_function(FunctionName=function_name)
        code_location = response['Code']['Location']
        
        # Download the zip file
        import urllib.request
        zip_path = os.path.join(temp_dir, f"{function_name}.zip")
        urllib.request.urlretrieve(code_location, zip_path)
        
        # Extract zip
        extract_path = os.path.join(temp_dir, function_name)
        os.makedirs(extract_path, exist_ok=True)
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_path)
        
        return extract_path
    except Exception as e:
        print(f"  Warning: Could not download code for {function_name}: {e}")
        return None

def check_python312_issues(code_path):
    """Check for known Python 3.12 compatibility issues"""
    issues = []
    
    # Known problematic imports/patterns in Python 3.12
    deprecated_modules = [
        'distutils',
        'imp',
        'asynchat',
        'asyncore',
        'smtpd'
    ]
    
    warning_patterns = [
        ('from distutils', 'distutils is removed in Python 3.12'),
        ('import distutils', 'distutils is removed in Python 3.12'),
        ('from imp import', 'imp module is removed in Python 3.12'),
        ('import imp', 'imp module is removed in Python 3.12'),
        ('asynchat', 'asynchat is removed in Python 3.12'),
        ('asyncore', 'asyncore is removed in Python 3.12'),
    ]
    
    try:
        for py_file in Path(code_path).rglob('*.py'):
            try:
                with open(py_file, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    for pattern, message in warning_patterns:
                        if pattern in content:
                            issues.append({
                                'file': str(py_file.relative_to(code_path)),
                                'issue': message,
                                'severity': 'HIGH'
                            })
            except Exception as e:
                continue
    except Exception as e:
        print(f"  Warning during static analysis: {e}")
    
    return issues

def run_vermin(code_path):
    """Run vermin to detect minimum Python version"""
    try:
        result = subprocess.run(
            ['vermin', '-t=3.12', '--no-tips', code_path],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        output = result.stdout + result.stderr
        
        # Parse vermin output
        if 'Minimum required versions' in output:
            for line in output.split('\n'):
                if 'Minimum required versions' in line:
                    return {
                        'compatible': '3.12' in line or result.returncode == 0,
                        'output': output,
                        'min_version': line.strip()
                    }
        
        return {
            'compatible': result.returncode == 0,
            'output': output,
            'min_version': 'Unknown'
        }
    except subprocess.TimeoutExpired:
        return {'compatible': None, 'output': 'Vermin timed out', 'min_version': 'Timeout'}
    except Exception as e:
        return {'compatible': None, 'output': str(e), 'min_version': 'Error'}

def check_requirements_compatibility(code_path):
    """Check if requirements.txt packages are compatible with Python 3.12"""
    req_file = os.path.join(code_path, 'requirements.txt')
    issues = []
    
    if os.path.exists(req_file):
        try:
            with open(req_file, 'r') as f:
                packages = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            
            # Known problematic packages (you can expand this list)
            known_issues = {
                'distutils': 'Removed in Python 3.12',
                'imp': 'Removed in Python 3.12',
            }
            
            for pkg in packages:
                pkg_name = pkg.split('==')[0].split('>=')[0].split('<=')[0].strip()
                if pkg_name in known_issues:
                    issues.append({
                        'package': pkg,
                        'issue': known_issues[pkg_name]
                    })
        except Exception as e:
            print(f"  Warning: Could not analyze requirements.txt: {e}")
    
    return issues

def analyze_function_compatibility(function_name, lambda_client, temp_dir, tools_available):
    """Perform comprehensive compatibility analysis on a Lambda function"""
    print(f"  Analyzing {function_name}...")
    
    result = {
        'download_success': False,
        'static_issues': [],
        'vermin_result': None,
        'requirements_issues': [],
        'compatibility_score': 'UNKNOWN',
        'recommendations': []
    }
    
    # Download code
    code_path = download_lambda_code(function_name, lambda_client, temp_dir)
    if not code_path:
        result['recommendations'].append('Could not download code for analysis')
        return result
    
    result['download_success'] = True
    
    # Static analysis for known issues
    result['static_issues'] = check_python312_issues(code_path)
    
    # Run vermin if available
    if tools_available.get('vermin'):
        result['vermin_result'] = run_vermin(code_path)
    
    # Check requirements.txt
    result['requirements_issues'] = check_requirements_compatibility(code_path)
    
    # Calculate compatibility score
    high_issues = len([i for i in result['static_issues'] if i['severity'] == 'HIGH'])
    req_issues = len(result['requirements_issues'])
    
    if high_issues > 0 or req_issues > 0:
        result['compatibility_score'] = 'HIGH RISK'
        result['recommendations'].append('Manual code review required before upgrade')
    elif result['vermin_result'] and result['vermin_result']['compatible'] is False:
        result['compatibility_score'] = 'MEDIUM RISK'
        result['recommendations'].append('Review vermin output for compatibility details')
    elif result['static_issues']:
        result['compatibility_score'] = 'MEDIUM RISK'
        result['recommendations'].append('Review static analysis warnings')
    else:
        result['compatibility_score'] = 'LOW RISK'
        result['recommendations'].append('Safe to upgrade with testing')
    
    return result

def analyze_lambda_runtimes(output_csv='lambda_compatibility_report.csv', region=None, check_compatibility=True):
    """Analyze Lambda functions and check Python 3.12 compatibility"""
    
    if region:
        lambda_client = boto3.client('lambda', region_name=region)
    else:
        lambda_client = boto3.client('lambda')
    
    print("\nRetrieving Lambda functions...")
    functions = get_all_lambda_functions(region)
    
    if not functions:
        print("No Lambda functions found in this region.")
        return [], {'total': 0, 'python_39_below': 0, 'cloudformation_managed': 0, 'requires_manual_update': 0}
    
    # Check for compatibility tools
    tools_available = check_compatibility_tools() if check_compatibility else {}
    
    # Track statistics
    stats = {
        'total': len(functions),
        'python_39_below': 0,
        'cloudformation_managed': 0,
        'requires_manual_update': 0,
        'high_risk': 0,
        'medium_risk': 0,
        'low_risk': 0
    }
    
    results = []
    runtime_counts = defaultdict(int)
    
    # Create temp directory for code downloads
    temp_dir = tempfile.mkdtemp() if check_compatibility else None
    
    try:
        for idx, func in enumerate(functions, 1):
            runtime = func.get('Runtime', 'N/A')
            func_name = func['FunctionName']
            
            runtime_counts[runtime] += 1
            
            # Check if Python runtime 3.9 or below
            if runtime.startswith('python'):
                version = runtime.replace('python', '')
                
                try:
                    version_float = float(version)
                    if version_float <= 3.9:
                        stats['python_39_below'] += 1
                        print(f"\n[{idx}/{len(functions)}] Found: {func_name} ({runtime})")
                        
                        # Check if CloudFormation managed
                        is_cfn, stack_name = check_cloudformation_managed(func_name, lambda_client)
                        
                        if is_cfn:
                            stats['cloudformation_managed'] += 1
                        else:
                            stats['requires_manual_update'] += 1
                        
                        # Compatibility analysis
                        compat_result = None
                        if check_compatibility:
                            compat_result = analyze_function_compatibility(
                                func_name, lambda_client, temp_dir, tools_available
                            )
                            
                            # Update risk stats
                            if compat_result['compatibility_score'] == 'HIGH RISK':
                                stats['high_risk'] += 1
                            elif compat_result['compatibility_score'] == 'MEDIUM RISK':
                                stats['medium_risk'] += 1
                            elif compat_result['compatibility_score'] == 'LOW RISK':
                                stats['low_risk'] += 1
                        
                        # Convert sizes to readable formats
                        code_size_bytes = func.get('CodeSize', 0)
                        code_size_kb = round(code_size_bytes / 1024, 2) if code_size_bytes else 0
                        
                        memory_size_mb = func.get('MemorySize', 0)
                        
                        results.append({
                            'FunctionName': func_name,
                            'Runtime': runtime,
                            'LastModified': func.get('LastModified', 'N/A'),
                            'CloudFormationManaged': 'Yes' if is_cfn else 'No',
                            'StackName': stack_name if is_cfn else 'N/A',
                            'CompatibilityScore': compat_result['compatibility_score'] if compat_result else 'NOT CHECKED',
                            'StaticIssuesCount': len(compat_result['static_issues']) if compat_result else 0,
                            'StaticIssues': json.dumps(compat_result['static_issues']) if compat_result else '[]',
                            'RequirementsIssues': json.dumps(compat_result['requirements_issues']) if compat_result else '[]',
                            'Recommendations': '; '.join(compat_result['recommendations']) if compat_result else '',
                            'VerminCompatible': compat_result['vermin_result']['compatible'] if compat_result and compat_result['vermin_result'] else 'N/A',
                            'CodeSize_KB': code_size_kb,
                            'MemorySize_MB': memory_size_mb,
                            'Timeout': func.get('Timeout', 'N/A'),
                            'FunctionArn': func.get('FunctionArn', 'N/A')
                        })
                except ValueError:
                    pass
    finally:
        # Cleanup temp directory
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
    
    # Sort results by risk level and function name
    risk_order = {'HIGH RISK': 0, 'MEDIUM RISK': 1, 'LOW RISK': 2, 'UNKNOWN': 3, 'NOT CHECKED': 4}
    results.sort(key=lambda x: (risk_order.get(x['CompatibilityScore'], 99), x['FunctionName']))
    
    # Print summary
    print("\n" + "="*70)
    print("LAMBDA RUNTIME ANALYSIS SUMMARY")
    print("="*70)
    print(f"Total Lambda functions: {stats['total']}")
    print(f"Functions with Python 3.9 or below: {stats['python_39_below']}")
    print(f"  - CloudFormation managed: {stats['cloudformation_managed']}")
    print(f"  - Console/CLI managed: {stats['requires_manual_update']}")
    
    if check_compatibility:
        print(f"\nCompatibility Assessment:")
        print(f"  - HIGH RISK: {stats['high_risk']}")
        print(f"  - MEDIUM RISK: {stats['medium_risk']}")
        print(f"  - LOW RISK: {stats['low_risk']}")
    
    print("\nRuntime Distribution:")
    for runtime, count in sorted(runtime_counts.items()):
        if runtime.startswith('python'):
            print(f"  {runtime}: {count}")
    print("="*70)
    
    # Write to CSV
    if results:
        with open(output_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=results[0].keys())
            writer.writeheader()
            writer.writerows(results)
        
        print(f"\nDetailed report saved to: {output_csv}")
        print(f"\nFunctions requiring update ({len(results)}):")
        print("-" * 90)
        
        for result in results:
            cfn_status = "⚠️  CFN" if result['CloudFormationManaged'] == 'Yes' else "✓ Manual"
            risk = result['CompatibilityScore']
            risk_icon = "🔴" if risk == "HIGH RISK" else "🟡" if risk == "MEDIUM RISK" else "🟢" if risk == "LOW RISK" else "⚪"
            print(f"{cfn_status:10} | {risk_icon} {risk:12} | {result['Runtime']:12} | {result['FunctionName']}")
    else:
        print("\n✓ No Lambda functions found using Python 3.9 or below!")
    
    return results, stats

if __name__ == "__main__":
    print("Lambda Python 3.12 Compatibility Analyzer")
    print("="*70)
    
    # Check AWS credentials first
    if not check_aws_credentials():
        sys.exit(1)
    
    # Get region
    region = os.environ.get('AWS_DEFAULT_REGION', os.environ.get('AWS_REGION'))
    if region:
        print(f"  Region: {region}")
    else:
        print("  Region: Using default from AWS config")
    
    # Check if user wants to skip compatibility checks
    check_compat = True
    if '--no-compat-check' in sys.argv:
        check_compat = False
        print("\n⚠️  Skipping compatibility checks (--no-compat-check flag detected)")
    
    print("="*70)
    
    try:
        results, stats = analyze_lambda_runtimes(region=region, check_compatibility=check_compat)
        
        if stats['python_39_below'] > 0:
            print("\n" + "="*70)
            print("ACTION ITEMS:")
            print("="*70)
            
            if check_compat:
                if stats['high_risk'] > 0:
                    print(f"🔴 HIGH PRIORITY: Review {stats['high_risk']} high-risk functions before upgrade")
                if stats['medium_risk'] > 0:
                    print(f"🟡 MEDIUM PRIORITY: Test {stats['medium_risk']} medium-risk functions thoroughly")
                if stats['low_risk'] > 0:
                    print(f"🟢 LOW RISK: {stats['low_risk']} functions appear safe to upgrade")
                print()
            
            if stats['requires_manual_update'] > 0:
                print(f"1. Update {stats['requires_manual_update']} console/CLI-managed functions directly")
            if stats['cloudformation_managed'] > 0:
                print(f"2. Update {stats['cloudformation_managed']} CloudFormation templates and redeploy")
            
            print("\nRecommended target runtime: python3.12")
            print("="*70)
    except KeyboardInterrupt:
        print("\n\nAnalysis interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nError during analysis: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
