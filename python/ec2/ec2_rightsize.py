#!/usr/bin/env python3
"""
EC2 Rightsizing Analyzer
Scans EC2 instances and CloudWatch metrics to provide rightsizing recommendations
"""

import csv
import boto3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone


class EC2RightsizingAnalyzer:
    def __init__(self, region='us-east-1', days_to_analyze=30):
        self.ec2 = boto3.client('ec2', region_name=region)
        self.cloudwatch = boto3.client('cloudwatch', region_name=region)
        self.region = region
        self.days_to_analyze = days_to_analyze
        
    def get_all_instances(self):
        """Retrieve all EC2 instances"""
        instances = []
        paginator = self.ec2.get_paginator('describe_instances')
        
        for page in paginator.paginate():
            for reservation in page['Reservations']:
                for instance in reservation['Instances']:
                    if instance['State']['Name'] != 'terminated':
                        instances.append(instance)
        
        return instances
    
    def get_cloudwatch_metrics(self, instance_id, metric_name, stat='Average'):
        """Get CloudWatch metrics for an instance"""
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=self.days_to_analyze)
        
        try:
            response = self.cloudwatch.get_metric_statistics(
                Namespace='AWS/EC2',
                MetricName=metric_name,
                Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
                StartTime=start_time,
                EndTime=end_time,
                Period=3600,  # 1 hour periods
                Statistics=[stat]
            )
            
            if response['Datapoints']:
                values = [dp[stat] for dp in response['Datapoints']]
                return {
                    'avg': statistics.mean(values),
                    'max': max(values),
                    'p95': statistics.quantiles(values, n=20)[18] if len(values) > 1 else values[0]
                }
            return None
        except Exception as e:
            print(f"Error getting metrics for {instance_id}: {e}")
            return None
    
    def get_instance_pricing(self, instance_type):
        """Estimate hourly cost (simplified - actual pricing varies by region and contract)"""
        # This is a simplified pricing model. For accurate pricing, use AWS Price List API
        pricing_map = {
            't2.micro': 0.0116, 't2.small': 0.023, 't2.medium': 0.0464,
            't3.micro': 0.0104, 't3.small': 0.0208, 't3.medium': 0.0416,
            'm5.large': 0.096, 'm5.xlarge': 0.192, 'm5.2xlarge': 0.384,
            'c5.large': 0.085, 'c5.xlarge': 0.17, 'c5.2xlarge': 0.34,
            'r5.large': 0.126, 'r5.xlarge': 0.252, 'r5.2xlarge': 0.504
        }
        return pricing_map.get(instance_type, 0.10)  # Default estimate
    
    def suggest_rightsize(self, instance_type, cpu_avg, memory_avg):
        """Suggest rightsizing based on utilization"""
        # Instance family mapping (simplified)
        downsize_map = {
            't3.medium': 't3.small',
            't3.small': 't3.micro',
            'm5.2xlarge': 'm5.xlarge',
            'm5.xlarge': 'm5.large',
            'm5.large': 't3.medium',
            'c5.2xlarge': 'c5.xlarge',
            'c5.xlarge': 'c5.large',
            'c5.large': 't3.medium',
            'r5.2xlarge': 'r5.xlarge',
            'r5.xlarge': 'r5.large',
            'r5.large': 'm5.large'
        }
        
        recommendations = []
        potential_savings = 0
        
        # CPU-based recommendation
        if cpu_avg < 10:
            recommendations.append("Very low CPU usage - consider stopping or downsizing")
            if instance_type in downsize_map:
                new_type = downsize_map[instance_type]
                current_cost = self.get_instance_pricing(instance_type)
                new_cost = self.get_instance_pricing(new_type)
                potential_savings = (current_cost - new_cost) * 730  # Monthly
                return f"Downsize to {new_type}", potential_savings
        elif cpu_avg < 25:
            recommendations.append("Low CPU usage - good candidate for downsizing")
            if instance_type in downsize_map:
                new_type = downsize_map[instance_type]
                current_cost = self.get_instance_pricing(instance_type)
                new_cost = self.get_instance_pricing(new_type)
                potential_savings = (current_cost - new_cost) * 730
                return f"Consider downsizing to {new_type}", potential_savings
        elif cpu_avg > 70:
            return "CPU usage high - current size appropriate or consider upsizing", 0
        else:
            return "Usage appears optimal", 0
        
        return "Review manually", 0
    
    def analyze_instances(self):
        """Main analysis function"""
        instances = self.get_all_instances()
        results = []
        
        print(f"Analyzing {len(instances)} instances...")
        
        for idx, instance in enumerate(instances):
            instance_id = instance['InstanceId']
            instance_type = instance['InstanceType']
            state = instance['State']['Name']
            
            # Get instance name
            name = 'N/A'
            if 'Tags' in instance:
                for tag in instance['Tags']:
                    if tag['Key'] == 'Name':
                        name = tag['Value']
                        break
            
            print(f"Processing {idx+1}/{len(instances)}: {instance_id} ({name})")
            
            # Get metrics
            cpu_metrics = self.get_cloudwatch_metrics(instance_id, 'CPUUtilization')
            
            if cpu_metrics and state == 'running':
                cpu_avg = cpu_metrics['avg']
                cpu_max = cpu_metrics['max']
                cpu_p95 = cpu_metrics['p95']
                
                recommendation, savings = self.suggest_rightsize(
                    instance_type, cpu_avg, None
                )
                
                current_monthly_cost = self.get_instance_pricing(instance_type) * 730
                
                results.append({
                    'InstanceId': instance_id,
                    'Name': name,
                    'InstanceType': instance_type,
                    'State': state,
                    'AvgCPU': f"{cpu_avg:.2f}%",
                    'MaxCPU': f"{cpu_max:.2f}%",
                    'P95CPU': f"{cpu_p95:.2f}%",
                    'CurrentMonthlyCost': f"${current_monthly_cost:.2f}",
                    'Recommendation': recommendation,
                    'PotentialMonthlySavings': f"${savings:.2f}",
                    'Region': self.region
                })
            else:
                results.append({
                    'InstanceId': instance_id,
                    'Name': name,
                    'InstanceType': instance_type,
                    'State': state,
                    'AvgCPU': 'N/A',
                    'MaxCPU': 'N/A',
                    'P95CPU': 'N/A',
                    'CurrentMonthlyCost': 'N/A',
                    'Recommendation': 'Stopped or no metrics available',
                    'PotentialMonthlySavings': '$0.00',
                    'Region': self.region
                })
        
        return results
    
    def save_to_csv(self, results, filename='ec2_rightsizing_report_30.csv'):
        """Save results to CSV"""
        if not results:
            print("No results to save")
            return
        
        keys = results[0].keys()
        with open(filename, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(results)
        
        print(f"\nReport saved to: {filename}")
        
        # Print summary
        total_savings = sum(
            float(r['PotentialMonthlySavings'].replace('$', ''))
            for r in results
        )
        print(f"\nTotal Potential Monthly Savings: ${total_savings:.2f}")
        print(f"Total Potential Annual Savings: ${total_savings * 12:.2f}")

def main():
    # Initialize analyzer
    # You can specify different regions or analyze multiple regions
    regions = ['us-east-1']  # Add more regions as needed
    
    all_results = []
    
    for region in regions:
        print(f"\n{'='*60}")
        print(f"Analyzing region: {region}")
        print(f"{'='*60}")
        
        analyzer = EC2RightsizingAnalyzer(region=region, days_to_analyze=14)
        results = analyzer.analyze_instances()
        all_results.extend(results)
    
    # Save combined results
    if all_results:
        analyzer.save_to_csv(all_results)
        
        print("\n" + "="*60)
        print("RECOMMENDATIONS SUMMARY")
        print("="*60)
        print("\nKey Findings:")
        print("1. Review instances with <10% average CPU utilization")
        print("2. Consider stopping instances that are running but unused")
        print("3. Implement auto-scaling for variable workloads")
        print("4. Use Reserved Instances or Savings Plans for steady workloads")
        print("5. Consider Spot Instances for fault-tolerant workloads")

if __name__ == '__main__':
    main()
