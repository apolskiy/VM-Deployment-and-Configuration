# Backend web server: Apache serving the bundled static site with a per-host
# identity header and meta tag. One image serves every backend; docker-compose
# sets BACKEND_HOST per container.
FROM httpd:2.4

COPY docker/backend/backend.conf /usr/local/apache2/conf/extra/backend.conf
COPY docker/backend/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/site/ /usr/local/apache2/htdocs/

# Append our vhost to the stock config and make the entrypoint executable.
RUN echo "Include conf/extra/backend.conf" >> /usr/local/apache2/conf/httpd.conf \
    && chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
