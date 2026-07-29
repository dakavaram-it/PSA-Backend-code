function notFoundHandler(req, res) {
  return res.status(404).json({
    error: `Route not found: ${req.method} ${req.originalUrl}`
  });
}

function errorHandler(error, req, res, next) {
  const statusCode = error.statusCode || 500;
  const message = error.message || 'Internal server error';

  return res.status(statusCode).json({
    error: message
  });
}

module.exports = {
  notFoundHandler,
  errorHandler
};


