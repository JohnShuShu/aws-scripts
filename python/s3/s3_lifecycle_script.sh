#!/usr/bin/env bash
# Apply S3 lifecycle policies to all buckets with comprehensive logging
# Handles existing policies, errors, and generates detailed reports
# Preview what would happen
# ./script.sh --dry-run

# Apply policy with backups
# ./script.sh --file lifecycle.json

# Merge with existing policies
# ./script.sh --merge

# Apply without backing up existing policies
# ./script.sh --no-backup

set -euo pipefail

# Configuration
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULTS_FILE="lifecycle_results_${TIMESTAMP}.csv"
LOG_FILE="lifecycle_log_${TIMESTAMP}.txt"
BACKUP_DIR="lifecycle_backups_${TIMESTAMP}"
LIFECYCLE_POLICY_FILE="lifecycle.json"
DRY_RUN=false
BACKUP_EXISTING=true
MERGE_POLICIES=false

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Usage function
usage() {
    cat << EOF
Usage: $0 [OPTIONS]

Apply lifecycle policies to S3 buckets with detailed logging and error handling.

Options:
    -f, --file FILE         Lifecycle policy JSON file (default: lifecycle.json)
    -d, --dry-run           Preview changes without applying them
    -n, --no-backup         Don't backup existing lifecycle policies
    -m, --merge             Merge with existing policies instead of replacing
    -h, --help              Show this help message

Examples:
    $0                                          # Apply lifecycle.json to all buckets
    $0 --file my-policy.json                    # Use custom policy file
    $0 --dry-run                                # Preview without applying
    $0 --merge                                  # Merge with existing policies

EOF
    exit 0
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -f|--file)
            LIFECYCLE_POLICY_FILE="$2"
            shift 2
            ;;
        -d|--dry-run)
            DRY_RUN=true
            shift
            ;;
        -n|--no-backup)
            BACKUP_EXISTING=false
            shift
            ;;
        -m|--merge)
            MERGE_POLICIES=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            usage
            ;;
    esac
done

# Progress bar function
show_progress() {
    local current=$1
    local total=$2
    local width=50
    local percentage=$((current * 100 / total))
    local completed=$((width * current / total))
    local remaining=$((width - completed))
    
    printf "\r  ["
    printf "%${completed}s" | tr ' ' '█'
    printf "%${remaining}s" | tr ' ' '░'
    printf "] %3d%% (%d/%d)" "$percentage" "$current" "$total"
}

# Logging function
log() {
    local level=$1
    shift
    local message="$@"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] [$level] $message" | tee -a "$LOG_FILE"
}

# Colored output function
print_status() {
    local color=$1
    local message=$2
    echo -e "${color}${message}${NC}"
}

# Validate prerequisites
validate_prerequisites() {
    log "INFO" "Validating prerequisites..."
    
    # Check if AWS CLI is installed
    if ! command -v aws &> /dev/null; then
        print_status "$RED" "❌ AWS CLI is not installed"
        log "ERROR" "AWS CLI not found"
        exit 1
    fi
    
    # Check if jq is installed
    if ! command -v jq &> /dev/null; then
        print_status "$RED" "❌ jq is not installed (required for policy merging)"
        log "ERROR" "jq not found"
        exit 1
    fi
    
    # Check if lifecycle policy file exists
    if [[ ! -f "$LIFECYCLE_POLICY_FILE" ]]; then
        print_status "$RED" "❌ Lifecycle policy file not found: $LIFECYCLE_POLICY_FILE"
        log "ERROR" "Policy file not found: $LIFECYCLE_POLICY_FILE"
        exit 1
    fi
    
    # Validate JSON syntax
    if ! jq empty "$LIFECYCLE_POLICY_FILE" 2>/dev/null; then
        print_status "$RED" "❌ Invalid JSON in policy file: $LIFECYCLE_POLICY_FILE"
        log "ERROR" "Invalid JSON in policy file"
        exit 1
    fi
    
    # Check AWS credentials
    if ! aws sts get-caller-identity &> /dev/null; then
        print_status "$RED" "❌ AWS credentials not configured or invalid"
        log "ERROR" "AWS credentials check failed"
        exit 1
    fi
    
    print_status "$GREEN" "✅ All prerequisites validated"
    log "INFO" "Prerequisites validated successfully"
}

# Create backup directory if needed
setup_backup_dir() {
    if [[ "$BACKUP_EXISTING" == true ]]; then
        mkdir -p "$BACKUP_DIR"
        log "INFO" "Created backup directory: $BACKUP_DIR"
    fi
}

# Backup existing lifecycle policy
backup_lifecycle_policy() {
    local bucket=$1
    local backup_file="${BACKUP_DIR}/${bucket}_lifecycle.json"
    
    if aws s3api get-bucket-lifecycle-configuration --bucket "$bucket" --output json > "$backup_file" 2>/dev/null; then
        log "INFO" "Backed up existing policy for bucket: $bucket"
        echo "$backup_file"
        return 0
    else
        log "INFO" "No existing lifecycle policy for bucket: $bucket"
        return 1
    fi
}

# Merge lifecycle policies
merge_lifecycle_policies() {
    local existing_policy=$1
    local new_policy=$2
    
    # Extract rules from both policies and merge them
    local merged_rules=$(jq -s '
        .[0].Rules + .[1].Rules | 
        group_by(.ID) | 
        map(if length > 1 then .[1] else .[0] end)
    ' "$existing_policy" "$new_policy")
    
    # Create merged policy
    echo "{\"Rules\": $merged_rules}"
}

# Apply lifecycle policy to a bucket
apply_lifecycle_policy() {
    local bucket=$1
    local status="UNKNOWN"
    local message=""
    local had_existing_policy=false
    local backup_file=""
    
    print_status "$CYAN" "📦 Processing bucket: $bucket"
    log "INFO" "Processing bucket: $bucket"
    
    # Check bucket region and versioning status
    local region=$(aws s3api get-bucket-location --bucket "$bucket" --query 'LocationConstraint' --output text 2>/dev/null || echo "us-east-1")
    if [[ "$region" == "None" || -z "$region" ]]; then
        region="us-east-1"
    fi
    
    # Backup existing policy if enabled
    if [[ "$BACKUP_EXISTING" == true ]]; then
        if backup_lifecycle_policy "$bucket"; then
            had_existing_policy=true
            backup_file="${BACKUP_DIR}/${bucket}_lifecycle.json"
        fi
    else
        # Just check if policy exists
        if aws s3api get-bucket-lifecycle-configuration --bucket "$bucket" &>/dev/null; then
            had_existing_policy=true
        fi
    fi
    
    # Determine the policy to apply
    local policy_to_apply="$LIFECYCLE_POLICY_FILE"
    
    if [[ "$MERGE_POLICIES" == true && "$had_existing_policy" == true ]]; then
        log "INFO" "Merging policies for bucket: $bucket"
        local merged_policy=$(merge_lifecycle_policies "$backup_file" "$LIFECYCLE_POLICY_FILE")
        local temp_policy="/tmp/${bucket}_merged_policy.json"
        echo "$merged_policy" > "$temp_policy"
        policy_to_apply="$temp_policy"
    fi
    
    # Apply or simulate policy application
    if [[ "$DRY_RUN" == true ]]; then
        status="DRY_RUN"
        message="Would apply lifecycle policy (dry run mode)"
        print_status "$YELLOW" "  ⚠️  DRY RUN: Would apply policy to $bucket"
    else
        if aws s3api put-bucket-lifecycle-configuration \
            --bucket "$bucket" \
            --lifecycle-configuration "file://${policy_to_apply}" \
            --region "$region" 2>&1 | tee -a "$LOG_FILE"; then
            
            status="SUCCESS"
            message="Lifecycle policy applied successfully"
            print_status "$GREEN" "  ✅ Successfully applied policy to $bucket"
            log "INFO" "Successfully applied policy to bucket: $bucket"
        else
            status="FAILED"
            message="Failed to apply lifecycle policy - check log for details"
            print_status "$RED" "  ❌ Failed to apply policy to $bucket"
            log "ERROR" "Failed to apply policy to bucket: $bucket"
        fi
    fi
    
    # Clean up temporary merged policy
    if [[ "$MERGE_POLICIES" == true && -f "/tmp/${bucket}_merged_policy.json" ]]; then
        rm -f "/tmp/${bucket}_merged_policy.json"
    fi
    
    # Return results as CSV row
    echo "$bucket,$region,$had_existing_policy,$status,$message,$(date '+%Y-%m-%d %H:%M:%S')"
}

# Main execution
main() {
    print_status "$BLUE" "╔════════════════════════════════════════════════════════════╗"
    print_status "$BLUE" "║  S3 Lifecycle Policy Application Tool                     ║"
    print_status "$BLUE" "╚════════════════════════════════════════════════════════════╝"
    echo
    
    log "INFO" "Starting lifecycle policy application"
    log "INFO" "Policy file: $LIFECYCLE_POLICY_FILE"
    log "INFO" "Dry run mode: $DRY_RUN"
    log "INFO" "Backup existing: $BACKUP_EXISTING"
    log "INFO" "Merge policies: $MERGE_POLICIES"
    
    # Validate prerequisites
    validate_prerequisites
    
    # Setup backup directory
    if [[ "$BACKUP_EXISTING" == true ]]; then
        setup_backup_dir
    fi
    
    # Display policy content
    print_status "$YELLOW" "📋 Lifecycle Policy to Apply:"
    cat "$LIFECYCLE_POLICY_FILE"
    echo
    
    if [[ "$DRY_RUN" == false ]]; then
        read -p "⚠️  This will apply the policy to ALL buckets. Continue? (yes/no): " confirm
        if [[ "$confirm" != "yes" ]]; then
            print_status "$YELLOW" "Operation cancelled by user"
            log "INFO" "Operation cancelled by user"
            exit 0
        fi
    fi
    
    # Create CSV header
    echo "Bucket,Region,HadExistingPolicy,Status,Message,Timestamp" > "$RESULTS_FILE"
    
    # Get all buckets
    print_status "$BLUE" "🔍 Fetching bucket list..."
    local buckets=$(aws s3api list-buckets --query "Buckets[].Name" --output text)
    
    if [[ -z "$buckets" ]]; then
        print_status "$RED" "❌ No S3 buckets found in this account"
        log "ERROR" "No buckets found"
        exit 1
    fi
    
    local bucket_count=$(echo "$buckets" | wc -w)
    print_status "$GREEN" "✅ Found $bucket_count buckets"
    log "INFO" "Found $bucket_count buckets to process"
    echo
    
    
    local success_count=0
    local fail_count=0
    local dry_run_count=0
    local current=0
    
    for bucket in $buckets; do
        ((current++))
        
        echo -e "\n${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${CYAN}[$current/$bucket_count] Processing: $bucket${NC}"
        echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        
        # Force output flush
        exec 1>&1
        
        result=$(apply_lifecycle_policy "$bucket")
        echo "$result" >> "$RESULTS_FILE"
        
        status=$(echo "$result" | cut -d',' -f4 | tr -d '"')
        
        case "$status" in
            SUCCESS)
                ((success_count++))
                echo -e "${GREEN}✅ Success${NC}"
                ;;
            FAILED)
                ((fail_count++))
                echo -e "${RED}❌ Failed${NC}"
                ;;
            DRY_RUN)
                ((dry_run_count++))
                echo -e "${YELLOW}⚠️  Dry run${NC}"
                ;;
        esac
        
        echo
    done
    
    # Summary
    print_status "$BLUE" "╔════════════════════════════════════════════════════════════╗"
    print_status "$BLUE" "║  Summary                                                   ║"
    print_status "$BLUE" "╚════════════════════════════════════════════════════════════╝"
    echo
    print_status "$CYAN" "📊 Total buckets processed: $bucket_count"
    
    if [[ "$DRY_RUN" == true ]]; then
        print_status "$YELLOW" "⚠️  Dry run completed: $dry_run_count buckets"
    else
        print_status "$GREEN" "✅ Successful: $success_count"
        print_status "$RED" "❌ Failed: $fail_count"
    fi
    
    echo
    print_status "$CYAN" "📄 Results saved to: $RESULTS_FILE"
    print_status "$CYAN" "📝 Log file: $LOG_FILE"
    
    if [[ "$BACKUP_EXISTING" == true && -d "$BACKUP_DIR" ]]; then
        local backup_count=$(ls -1 "$BACKUP_DIR" 2>/dev/null | wc -l)
        if [[ $backup_count -gt 0 ]]; then
            print_status "$CYAN" "💾 Backups saved to: $BACKUP_DIR ($backup_count policies)"
        fi
    fi
    
    echo
    
    # Display results table
    if command -v column &> /dev/null; then
        print_status "$YELLOW" "📋 Detailed Results:"
        echo
        column -t -s',' "$RESULTS_FILE"
    fi
    
    log "INFO" "Script execution completed"
    log "INFO" "Success: $success_count, Failed: $fail_count"
    
    # Exit with appropriate code
    if [[ $fail_count -gt 0 && "$DRY_RUN" == false ]]; then
        exit 1
    else
        exit 0
    fi
}

# Run main function
main
