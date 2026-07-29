const express = require('express');
const healthController = require('../controllers/healthController');
const asyncHandler = require('../middleware/asyncHandler');

const router = express.Router();

router.get('/health', asyncHandler(healthController.health));

module.exports = router;

