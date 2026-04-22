FROM python:3.13-slim

# Set the working directory in the container
WORKDIR /usr/src/app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entrypoint script
# Note: If you mount a volume to /usr/src/app, this file in the image
# will be hidden by the file on your host.
COPY docker_entrypoint.sh .

# Ensure execution permissions
RUN chmod +x docker_entrypoint.sh

# Make port 5000 available to the world outside this container
EXPOSE 5000

# Define environment variable for unbuffered logging (helps see logs instantly)
ENV PYTHONUNBUFFERED=1

# Run the entrypoint script
ENTRYPOINT ["./docker_entrypoint.sh"]
