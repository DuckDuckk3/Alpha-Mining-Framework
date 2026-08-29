```bash
#!/bin/bash
# WorldQuant Alpha Miner - Docker Deployment Script
# Usage:
#   ./deploy.sh          # Start the miner
#   ./deploy.sh stop     # Stop the miner
#   ./deploy.sh logs     # View logs
#   ./deploy.sh submit   # Submit factors
#   ./deploy.sh status   # Check status

cd "$(dirname "$0")" || exit

case "${1:-start}" in
    start)
        echo "=========================================="
        echo "  Starting WorldQuant Alpha Miner"
        echo "=========================================="
        echo ""

        # Check .env file
        if [ ! -f .env ]; then
            echo "Error: .env file does not exist"
            echo "Please create a .env file and configure the credentials first"
            exit 1
        fi

        # Build and start
        docker compose up -d --build miner

        echo ""
        echo "Miner started!"
        echo ""
        echo "Common commands:"
        echo "  View logs: docker compose logs -f miner"
        echo "  Stop miner: ./deploy.sh stop"
        echo "  Check status: ./deploy.sh status"
        echo "=========================================="
        ;;

    stop)
        echo "Stopping miner..."
        docker compose down
        echo "Stopped"
        ;;

    logs)
        docker compose logs -f miner
        ;;

    status)
        echo "=========================================="
        echo "  WorldQuant Alpha Miner Status"
        echo "=========================================="
        echo ""

        # Container status
        docker compose ps

        echo ""
        echo "Recent logs:"
        docker compose logs --tail=20 miner
        ;;

    submit)
        shift  # Remove 'submit' from args
        if [ -z "$1" ]; then
            echo "Submitting all unsubmitted factors..."
            docker compose run --rm submit python submit_alpha.py
        else
            echo "Submitting factors: $*"
            docker compose run --rm submit python submit_alpha.py "$@"
        fi
        ;;

    *)
        echo "Usage: $0 {start|stop|logs|status|submit}"
        exit 1
        ;;
esac
```
