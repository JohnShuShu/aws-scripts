#!/usr/bin/env bash
# List in-progress multipart uploads for all S3 buckets in your account
# Writes details to a CSV file including name, creation date, last updated, size, etc.
# Requires: AWS CLI v2 and proper credentials (with s3:ListBucketMultipartUploads)

set -euo pipefail

# Output CSV file
OUTPUT_FILE="multipart_uploads_report_$(date +%Y%m%d_%H%M%S).csv"

echo "🔍 Listing multipart uploads for all S3 buckets..."
echo "📄 Output file: $OUTPUT_FILE"
echo

# Write CSV header
echo "Bucket,Key,UploadId,Initiated,Owner,StorageClass,PartCount,TotalSize(MB)" > "$OUTPUT_FILE"

# Get all bucket names
buckets=$(aws s3api list-buckets --query "Buckets[].Name" --output text)

# Exit if no buckets
if [[ -z "$buckets" ]]; then
  echo "No S3 buckets found in this account."
  exit 0
fi

total_uploads=0

# Loop through each bucket
for bucket in $buckets; do
  echo "🪣 Bucket: $bucket"
  
  # List multipart uploads (incomplete)
  uploads=$(aws s3api list-multipart-uploads --bucket "$bucket" --output json 2>/dev/null || echo '{"Uploads":[]}')

  # Check if there are uploads
  upload_count=$(echo "$uploads" | jq '.Uploads | length // 0')

  if [[ "$upload_count" -gt 0 ]]; then
    echo "⚠️  Found $upload_count incomplete multipart uploads"
    
    # Process each upload using jq array iteration
    echo "$uploads" | jq -c '.Uploads[]' | while IFS= read -r upload; do
      key=$(echo "$upload" | jq -r '.Key')
      upload_id=$(echo "$upload" | jq -r '.UploadId')
      initiated=$(echo "$upload" | jq -r '.Initiated')
      owner=$(echo "$upload" | jq -r '.Owner.DisplayName // .Owner.ID // "N/A"')
      storage_class=$(echo "$upload" | jq -r '.StorageClass // "STANDARD"')
      
      # Get parts information to calculate total size
      parts=$(aws s3api list-parts --bucket "$bucket" --key "$key" --upload-id "$upload_id" --output json 2>/dev/null || echo '{"Parts":[]}')
      
      part_count=$(echo "$parts" | jq '.Parts | length // 0')
      total_size_bytes=$(echo "$parts" | jq '[.Parts[]?.Size // 0] | add // 0')
      
      # Calculate MB (handle case where bc might not be available)
      if command -v bc &> /dev/null; then
        total_size_mb=$(echo "scale=2; $total_size_bytes / 1048576" | bc -l)
      else
        total_size_mb=$(awk "BEGIN {printf \"%.2f\", $total_size_bytes / 1048576}")
      fi
      
      # Escape commas and quotes in key for CSV
      key_escaped=$(echo "$key" | sed 's/"/""/g')
      
      # Write to CSV (quote fields that might contain special characters)
      echo "\"$bucket\",\"$key_escaped\",\"$upload_id\",\"$initiated\",\"$owner\",\"$storage_class\",$part_count,$total_size_mb" >> "$OUTPUT_FILE"
    done
    
    total_uploads=$((total_uploads + upload_count))
  else
    echo "✅ No incomplete multipart uploads found."
  fi
  echo
done

echo "🎯 Done."
echo "📊 Total incomplete uploads found: $total_uploads"
echo "💾 Results saved to: $OUTPUT_FILE"

# Display summary
if [[ $total_uploads -gt 0 ]]; then
  echo
  echo "📋 Summary (top 10 by size):"
  echo "Bucket | Key | Upload ID | Initiated | Owner | Storage Class | Parts | Size(MB)"
  echo "-------|-----|-----------|-----------|-------|---------------|-------|----------"
  tail -n +2 "$OUTPUT_FILE" | sort -t',' -k8 -rn | head -n 10 | while IFS=, read -r bucket key upload_id initiated owner storage_class parts size; do
    # Truncate long values for display
    key_short=$(echo "$key" | cut -c1-30)
    upload_short=$(echo "$upload_id" | cut -c1-20)
    printf "%-15s | %-30s | %-20s | %-19s | %-15s | %-13s | %5s | %8s\n" \
      "$bucket" "$key_short" "$upload_short" "$initiated" "$owner" "$storage_class" "$parts" "$size"
  done
fi
