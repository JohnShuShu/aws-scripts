#!/usr/bin/env python3
"""
S3 Cost Optimization Analyzer
Analyzes S3 buckets to identify cost-saving opportunities including:
- Objects that can be moved to tiered storage
- Old objects that can be deleted
- Incomplete multipart uploads
"""

import json
import boto3
import argparse
import subprocess
from collections import defaultdict
from botocore.exceptions import ClientError
from datetime import datetime, timezone, timedelta


class S3CostAnalyzer:
    def __init__(self, region_name=None):
        self.s3_client = boto3.client('s3', region_name=region_name)
        self.s3_resource = boto3.resource('s3', region_name=region_name)
        self.profile = "tss-sso"

        
        # Storage class pricing (approximate USD per GB per month)
        self.pricing = {
            'STANDARD': 0.023,
            'INTELLIGENT_TIERING': 0.023,
            'STANDARD_IA': 0.0125,
            'ONEZONE_IA': 0.01,
            'GLACIER_IR': 0.004,
            'GLACIER': 0.0036,
            'DEEP_ARCHIVE': 0.00099
        }

    def is_sso_session_valid(self) -> bool:
        """Return True if the AWS SSO session is still valid."""
        try:
            subprocess.run(
                ["aws", "sts", "get-caller-identity", "--profile", self.profile],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    def login_sso(self):
        """Run AWS SSO login for the given profile."""
        print(f"🔐 Logging in to AWS SSO profile: {self.profile} ...")
        subprocess.run(["aws", "sso", "login", "--profile", self.profile], check=True)
        print("✅ Login successful.")

    def get_all_buckets(self):
        """Retrieve all S3 buckets"""
        try:
            response = self.s3_client.list_buckets()
            return [bucket['Name'] for bucket in response['Buckets']]
        except ClientError as e:
            print(f"Error listing buckets: {e}")
            return []
    
    def analyze_bucket_objects(self, bucket_name, days_threshold=90):
        """Analyze objects in a bucket for cost optimization"""
        print(f"Analyzing bucket: {bucket_name}...")
        
        bucket = self.s3_resource.Bucket(bucket_name)
        analysis = {
            'bucket_name': bucket_name,
            'total_objects': 0,
            'total_size_gb': 0,
            'by_storage_class': defaultdict(lambda: {'count': 0, 'size_gb': 0}),
            'old_objects': [],
            'candidates_for_ia': [],
            'candidates_for_glacier': [],
            'by_prefix': defaultdict(lambda: {'count': 0, 'size_gb': 0}),
        }
        
        now = datetime.now(timezone.utc)
        threshold_date = now - timedelta(days=days_threshold)
        ia_threshold = now - timedelta(days=30)  # Objects older than 30 days for IA
        glacier_threshold = now - timedelta(days=180)  # Objects older than 180 days for Glacier
        
        try:

            if self.is_sso_session_valid():
                print(f"✅ AWS SSO session for profile '{self.profile}' is still valid.")
            else:
                print(f"⚠️  AWS SSO session for profile '{self.profile}' has expired.")
                self.login_sso()

            for obj in bucket.objects.all():
                size_gb = obj.size / (1024**3)
                storage_class = obj.storage_class if obj.storage_class else 'STANDARD'
                
                analysis['total_objects'] += 1
                analysis['total_size_gb'] += size_gb
                analysis['by_storage_class'][storage_class]['count'] += 1
                analysis['by_storage_class'][storage_class]['size_gb'] += size_gb
                
                # Analyze by prefix (folder)
                prefix = '/'.join(obj.key.split('/')[:-1]) if '/' in obj.key else 'root'
                analysis['by_prefix'][prefix]['count'] += 1
                analysis['by_prefix'][prefix]['size_gb'] += size_gb
                
                # Find old objects
                if obj.last_modified < threshold_date:
                    analysis['old_objects'].append({
                        'key': obj.key,
                        'size_gb': size_gb,
                        'last_modified': obj.last_modified.isoformat(),
                        'storage_class': storage_class,
                        'age_days': (now - obj.last_modified).days
                    })
                
                # Candidates for Intelligent-Tiering or Standard-IA
                if (storage_class == 'STANDARD' and 
                    obj.last_modified < ia_threshold and 
                    obj.size > 128 * 1024):  # Min size for IA
                    analysis['candidates_for_ia'].append({
                        'key': obj.key,
                        'size_gb': size_gb,
                        'last_modified': obj.last_modified.isoformat(),
                        'age_days': (now - obj.last_modified).days,
                        'potential_savings_monthly': size_gb * (self.pricing['STANDARD'] - self.pricing['STANDARD_IA'])
                    })
                
                # Candidates for Glacier
                if (storage_class in ['STANDARD', 'STANDARD_IA'] and 
                    obj.last_modified < glacier_threshold):
                    current_cost = self.pricing.get(storage_class, self.pricing['STANDARD'])
                    analysis['candidates_for_glacier'].append({
                        'key': obj.key,
                        'size_gb': size_gb,
                        'last_modified': obj.last_modified.isoformat(),
                        'age_days': (now - obj.last_modified).days,
                        'current_storage_class': storage_class,
                        'potential_savings_monthly': size_gb * (current_cost - self.pricing['GLACIER'])
                    })
        
        except ClientError as e:
            print(f"Error accessing bucket {bucket_name}: {e}")
            analysis['error'] = str(e)
        
        return analysis
    
    def analyze_multipart_uploads(self, bucket_name, days_threshold=7):
        """Analyze incomplete multipart uploads"""
        print(f"Analyzing multipart uploads for bucket: {bucket_name}...")
        
        multipart_analysis = {
            'bucket_name': bucket_name,
            'total_uploads': 0,
            'old_uploads': [],
            'estimated_storage_gb': 0
        }
        
        now = datetime.now(timezone.utc)
        threshold_date = now - timedelta(days=days_threshold)
        
        try:
            paginator = self.s3_client.get_paginator('list_multipart_uploads')
            pages = paginator.paginate(Bucket=bucket_name)
            
            for page in pages:
                if 'Uploads' in page:
                    for upload in page['Uploads']:
                        multipart_analysis['total_uploads'] += 1
                        initiated = upload['Initiated']
                        
                        if initiated < threshold_date:
                            # Get parts to estimate size
                            try:
                                parts_response = self.s3_client.list_parts(
                                    Bucket=bucket_name,
                                    Key=upload['Key'],
                                    UploadId=upload['UploadId']
                                )
                                
                                total_size = sum(part['Size'] for part in parts_response.get('Parts', []))
                                size_gb = total_size / (1024**3)
                                
                                multipart_analysis['old_uploads'].append({
                                    'key': upload['Key'],
                                    'upload_id': upload['UploadId'],
                                    'initiated': initiated.isoformat(),
                                    'age_days': (now - initiated).days,
                                    'size_gb': size_gb
                                })
                                
                                multipart_analysis['estimated_storage_gb'] += size_gb
                            except ClientError:
                                # If we can't list parts, just note the upload without size
                                multipart_analysis['old_uploads'].append({
                                    'key': upload['Key'],
                                    'upload_id': upload['UploadId'],
                                    'initiated': initiated.isoformat(),
                                    'age_days': (now - initiated).days,
                                    'size_gb': 0  # Unknown size
                                })
        
        except ClientError as e:
            print(f"Error analyzing multipart uploads for {bucket_name}: {e}")
            multipart_analysis['error'] = str(e)
        
        return multipart_analysis
    
    def generate_report(self, bucket_analyses, multipart_analyses, output_file='s3_cost_report.txt'):
        """Generate a comprehensive cost optimization report"""
        with open(output_file, 'w') as f:
            f.write("=" * 80 + "\n")
            f.write("S3 COST OPTIMIZATION REPORT\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n\n")
            
            # Summary across all buckets
            total_size = sum(a['total_size_gb'] for a in bucket_analyses)
            total_objects = sum(a['total_objects'] for a in bucket_analyses)
            total_ia_savings = sum(
                sum(obj['potential_savings_monthly'] for obj in a['candidates_for_ia'])
                for a in bucket_analyses
            )
            total_glacier_savings = sum(
                sum(obj['potential_savings_monthly'] for obj in a['candidates_for_glacier'])
                for a in bucket_analyses
            )
            total_multipart_storage = sum(m['estimated_storage_gb'] for m in multipart_analyses)
            
            f.write("EXECUTIVE SUMMARY\n")
            f.write("-" * 80 + "\n")
            f.write(f"Total Buckets Analyzed: {len(bucket_analyses)}\n")
            f.write(f"Total Objects: {total_objects:,}\n")
            f.write(f"Total Storage: {total_size:.2f} GB\n")
            f.write(f"Potential Monthly Savings (IA Migration): ${total_ia_savings:.2f}\n")
            f.write(f"Potential Monthly Savings (Glacier Migration): ${total_glacier_savings:.2f}\n")
            f.write(f"Storage in Incomplete Multipart Uploads: {total_multipart_storage:.2f} GB\n")
            f.write(f"Estimated Monthly Cost of Multipart Storage: ${total_multipart_storage * self.pricing['STANDARD']:.2f}\n")
            f.write("\n\n")
            
            # Per-bucket analysis
            for analysis in bucket_analyses:
                f.write("=" * 80 + "\n")
                f.write(f"BUCKET: {analysis['bucket_name']}\n")
                f.write("=" * 80 + "\n\n")
                
                if 'error' in analysis:
                    f.write(f"ERROR: {analysis['error']}\n\n")
                    continue
                
                f.write(f"Total Objects: {analysis['total_objects']:,}\n")
                f.write(f"Total Size: {analysis['total_size_gb']:.2f} GB\n\n")
                
                # Storage class breakdown
                f.write("STORAGE CLASS DISTRIBUTION\n")
                f.write("-" * 80 + "\n")
                for storage_class, data in sorted(analysis['by_storage_class'].items()):
                    monthly_cost = data['size_gb'] * self.pricing.get(storage_class, 0.023)
                    f.write(f"{storage_class:20s}: {data['count']:>8,} objects, "
                           f"{data['size_gb']:>10.2f} GB, ${monthly_cost:>8.2f}/month\n")
                f.write("\n")
                
                # Top prefixes (folders)
                f.write("TOP 10 PREFIXES BY SIZE\n")
                f.write("-" * 80 + "\n")
                sorted_prefixes = sorted(analysis['by_prefix'].items(), 
                                       key=lambda x: x[1]['size_gb'], reverse=True)[:10]
                for prefix, data in sorted_prefixes:
                    f.write(f"{prefix[:60]:60s}: {data['count']:>8,} objects, {data['size_gb']:>10.2f} GB\n")
                f.write("\n")
                
                # Candidates for Standard-IA
                if analysis['candidates_for_ia']:
                    f.write("CANDIDATES FOR STANDARD-IA (Top 20 by size)\n")
                    f.write("-" * 80 + "\n")
                    f.write(f"Total candidates: {len(analysis['candidates_for_ia'])}\n")
                    total_savings = sum(obj['potential_savings_monthly'] 
                                      for obj in analysis['candidates_for_ia'])
                    f.write(f"Potential monthly savings: ${total_savings:.2f}\n\n")
                    
                    sorted_ia = sorted(analysis['candidates_for_ia'], 
                                     key=lambda x: x['size_gb'], reverse=True)[:20]
                    for obj in sorted_ia:
                        f.write(f"  {obj['key'][:70]}\n")
                        f.write(f"    Size: {obj['size_gb']:.4f} GB, Age: {obj['age_days']} days, "
                               f"Savings: ${obj['potential_savings_monthly']:.2f}/month\n")
                    f.write("\n")
                
                # Candidates for Glacier
                if analysis['candidates_for_glacier']:
                    f.write("CANDIDATES FOR GLACIER (Top 20 by savings)\n")
                    f.write("-" * 80 + "\n")
                    f.write(f"Total candidates: {len(analysis['candidates_for_glacier'])}\n")
                    total_savings = sum(obj['potential_savings_monthly'] 
                                      for obj in analysis['candidates_for_glacier'])
                    f.write(f"Potential monthly savings: ${total_savings:.2f}\n\n")
                    
                    sorted_glacier = sorted(analysis['candidates_for_glacier'], 
                                          key=lambda x: x['potential_savings_monthly'], 
                                          reverse=True)[:20]
                    for obj in sorted_glacier:
                        f.write(f"  {obj['key'][:70]}\n")
                        f.write(f"    Size: {obj['size_gb']:.4f} GB, Age: {obj['age_days']} days, "
                               f"Current: {obj['current_storage_class']}, "
                               f"Savings: ${obj['potential_savings_monthly']:.2f}/month\n")
                    f.write("\n")
                
                # Old objects summary
                if analysis['old_objects']:
                    total_old_size = sum(obj['size_gb'] for obj in analysis['old_objects'])
                    f.write(f"OLD OBJECTS (>90 days, not accessed recently)\n")
                    f.write("-" * 80 + "\n")
                    f.write(f"Total old objects: {len(analysis['old_objects'])}\n")
                    f.write(f"Total size: {total_old_size:.2f} GB\n")
                    f.write("Review these objects for potential deletion\n\n")
                
                f.write("\n")
            
            # Multipart uploads analysis
            f.write("=" * 80 + "\n")
            f.write("INCOMPLETE MULTIPART UPLOADS\n")
            f.write("=" * 80 + "\n\n")
            
            for mp_analysis in multipart_analyses:
                f.write(f"Bucket: {mp_analysis['bucket_name']}\n")
                f.write("-" * 80 + "\n")
                
                if 'error' in mp_analysis:
                    f.write(f"ERROR: {mp_analysis['error']}\n\n")
                    continue
                
                f.write(f"Total incomplete uploads: {mp_analysis['total_uploads']}\n")
                f.write(f"Old uploads (>7 days): {len(mp_analysis['old_uploads'])}\n")
                f.write(f"Estimated storage: {mp_analysis['estimated_storage_gb']:.2f} GB\n")
                
                if mp_analysis['old_uploads']:
                    monthly_cost = mp_analysis['estimated_storage_gb'] * self.pricing['STANDARD']
                    f.write(f"Estimated monthly cost: ${monthly_cost:.2f}\n\n")
                    
                    f.write("Old incomplete uploads:\n")
                    for upload in sorted(mp_analysis['old_uploads'], 
                                       key=lambda x: x['age_days'], reverse=True)[:50]:
                        f.write(f"  {upload['key'][:70]}\n")
                        f.write(f"    Age: {upload['age_days']} days, Size: {upload['size_gb']:.4f} GB\n")
                        f.write(f"    Upload ID: {upload['upload_id']}\n")
                f.write("\n")
            
            # Recommendations
            f.write("=" * 80 + "\n")
            f.write("RECOMMENDATIONS\n")
            f.write("=" * 80 + "\n\n")
            f.write("1. LIFECYCLE POLICIES\n")
            f.write("   - Create lifecycle policies to automatically transition objects to IA after 30 days\n")
            f.write("   - Transition rarely accessed objects to Glacier after 180 days\n")
            f.write("   - Enable Intelligent-Tiering for objects with unpredictable access patterns\n\n")
            f.write("2. MULTIPART UPLOADS\n")
            f.write("   - Abort incomplete multipart uploads older than 7 days\n")
            f.write("   - Create lifecycle policy to automatically abort incomplete uploads\n\n")
            f.write("3. DATA CLEANUP\n")
            f.write("   - Review old objects (>90 days) for potential deletion\n")
            f.write("   - Implement data retention policies\n\n")
            f.write("4. MONITORING\n")
            f.write("   - Enable S3 Storage Lens for ongoing cost monitoring\n")
            f.write("   - Set up CloudWatch metrics for bucket-level monitoring\n\n")
        
        print(f"\nReport written to: {output_file}")
        
        # Also generate JSON for programmatic use
        json_output = output_file.replace('.txt', '.json')
        with open(json_output, 'w') as f:
            json.dump({
                'bucket_analyses': bucket_analyses,
                'multipart_analyses': multipart_analyses,
                'summary': {
                    'total_buckets': len(bucket_analyses),
                    'total_objects': total_objects,
                    'total_size_gb': total_size,
                    'potential_ia_savings_monthly': total_ia_savings,
                    'potential_glacier_savings_monthly': total_glacier_savings,
                    'multipart_storage_gb': total_multipart_storage
                }
            }, f, indent=2, default=str)
        print(f"JSON data written to: {json_output}")


def main():
    parser = argparse.ArgumentParser(description='Analyze S3 buckets for cost optimization')
    parser.add_argument('--buckets', nargs='+', help='Specific bucket names to analyze (default: all)')
    parser.add_argument('--region', default='us-east-1', help='AWS region')
    parser.add_argument('--output', default='s3_cost_report.txt', help='Output report file')
    parser.add_argument('--days-threshold', type=int, default=90, 
                       help='Age threshold for old objects (default: 90 days)')
    parser.add_argument('--multipart-days', type=int, default=7,
                       help='Age threshold for old multipart uploads (default: 7 days)')
    
    args = parser.parse_args()
    
    analyzer = S3CostAnalyzer(region_name=args.region)
    
    # Get bucket list
    if args.buckets:
        buckets = args.buckets
    else:
        buckets = analyzer.get_all_buckets()
    
    if not buckets:
        print("No buckets found or unable to list buckets")
        return
    
    print(f"Analyzing {len(buckets)} bucket(s)...\n")
    
    # Analyze each bucket
    bucket_analyses = []
    multipart_analyses = []
    
    for bucket in buckets:
        analysis = analyzer.analyze_bucket_objects(bucket, days_threshold=args.days_threshold)
        bucket_analyses.append(analysis)
        
        mp_analysis = analyzer.analyze_multipart_uploads(bucket, days_threshold=args.multipart_days)
        multipart_analyses.append(mp_analysis)
    
    # Generate report
    analyzer.generate_report(bucket_analyses, multipart_analyses, output_file=args.output)
    
    print("\nAnalysis complete!")


if __name__ == '__main__':
    main()
