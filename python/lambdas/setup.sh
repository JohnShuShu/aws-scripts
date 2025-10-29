# #!/bin/sh

# Lambda Python 3.12 Compatibility Analyzer - Setup Script
# This script sets up the environment and runs the analyzer

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color


# Setting up env vars and logging int to AWS.
# profile_name='tss-sso'
# export AWS_PROFILE=$profile_name
# export AWS_REGION="us-east-1"
# aws sso login --profile $profile_name

# Function to print colored output
print_header() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

# Check if Python is installed
print_header "Checking Python Installation"
if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version)
    print_success "Python installed: $PYTHON_VERSION"
else
    print_error "Python 3 is not installed. Please install Python 3.8 or higher."
    exit 1
fi

# Check if pip is installed
if command -v pip3 &> /dev/null; then
    print_success "pip3 is installed"
else
    print_error "pip3 is not installed. Please install pip3."
    exit 1
fi

# Create virtual environment (optional but recommended)
print_header "Setting Up Virtual Environment"
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    print_success "Virtual environment created"
else
    print_warning "Virtual environment already exists"
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate
print_success "Virtual environment activated"

# Upgrade pip
print_header "Upgrading pip"
pip install --upgrade pip --quiet
print_success "pip upgraded"

# Install required packages
print_header "Installing Required Packages"

echo "Installing boto3 (required for AWS)..."
pip install boto3 --quiet
print_success "boto3 installed"

echo "Installing compatibility analysis tools..."
pip install vermin pyupgrade pylint --quiet
print_success "Compatibility tools installed (vermin, pyupgrade, pylint)"

# Check AWS CLI
print_header "Checking AWS Configuration"
if command -v aws &> /dev/null; then
    print_success "AWS CLI is installed"
    
    # Check if AWS credentials are configured
    if aws sts get-caller-identity &> /dev/null; then
        print_success "AWS credentials are configured"
        AWS_ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
        AWS_REGION=$(aws configure get region 2>/dev/null || echo "not set")
        echo "  Account: $AWS_ACCOUNT"
        echo "  Region: $AWS_REGION"
    else
        print_warning "AWS credentials not configured"
        echo ""
        echo "Configure AWS credentials with one of these methods:"
        echo "  1. Run: aws configure"
        echo "  2. Set environment variables:"
        echo "     export AWS_ACCESS_KEY_ID=your_key"
        echo "     export AWS_SECRET_ACCESS_KEY=your_secret"
        echo "     export AWS_DEFAULT_REGION=us-east-1"
        echo "  3. Use AWS SSO: aws sso login --profile your-profile"
        echo ""
        read -p "Would you like to run 'aws configure' now? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            aws configure
        fi
    fi
else
    print_warning "AWS CLI not installed (optional but recommended)"
    echo "Install with: pip install awscli"
fi

# Create a requirements.txt file
print_header "Creating requirements.txt"
cat > requirements.txt << EOF
boto3>=1.28.0
vermin>=1.5.0
pyupgrade>=3.3.0
pylint>=2.17.0
EOF
print_success "requirements.txt created"

# Summary
print_header "Setup Complete!"
echo ""
echo "Installation Summary:"
echo "  ✓ Virtual environment: venv/"
echo "  ✓ Required packages installed"
echo "  ✓ Compatibility tools ready"
echo ""
echo -e "${GREEN}Next Steps:${NC}"
echo ""
echo "1. Ensure AWS credentials are configured (if not done above)"
echo ""
echo "2. Run the analyzer:"
echo -e "   ${YELLOW}python lambda_runtime_analyzer.py${NC}"
echo ""
echo "3. For quick scan without compatibility checks:"
echo -e "   ${YELLOW}python lambda_runtime_analyzer.py --no-compat-check${NC}"
echo ""
echo "4. To run in a specific region:"
echo -e "   ${YELLOW}export AWS_DEFAULT_REGION=us-west-2${NC}"
echo -e "   ${YELLOW}python lambda_runtime_analyzer.py${NC}"
echo ""
echo "5. View the generated report:"
echo -e "   ${YELLOW}cat lambda_compatibility_report.csv${NC}"
echo ""
echo -e "${BLUE}Tip:${NC} Keep this virtual environment activated for running the analyzer"
echo -e "     To activate later: ${YELLOW}source venv/bin/activate${NC}"
echo -e "     To deactivate: ${YELLOW}deactivate${NC}"
echo ""

# Ask if user wants to run the analyzer now
echo ""
read -p "Would you like to run the analyzer now? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if [ -f "lambda_runtime_analyzer.py" ]; then
        echo ""
        print_header "Running Lambda Analyzer"
        python lambda_runtime_analyzer.py
    else
        print_error "lambda_runtime_analyzer.py not found in current directory"
        echo "Please ensure the analyzer script is in the same directory as this setup script"
    fi
fi
