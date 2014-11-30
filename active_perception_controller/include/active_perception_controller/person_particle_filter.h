#ifndef PERSON_PARTICLE_FILTER_H
#define PERSON_PARTICLE_FILTER_H

#include <geometry_msgs/PoseArray.h>
#include <sensor_msgs/PointCloud.h>
#include <active_perception_controller/particle_filter.h>
#include <active_perception_controller/rfid_sensor_model.h>

#include <gsl/gsl_randist.h>
#include <vector>
#include <string>

using namespace std;

/** \brief Information for a single particle

*/
class PersonParticle : public Particle
{
public:
    // Pose represented by this sample
    vector<double> pose_;
    PersonParticle();
    PersonParticle(const PersonParticle &person_particle);
};

/**
  Class to implement a particle filter that estimate a person position
  */
class PersonParticleFilter : public ParticleFilter
{
public:
    PersonParticleFilter(int n_particles);
    PersonParticleFilter(int n_particles, nav_msgs::OccupancyGrid const* map, double sigma_pose, double rfid_map_res, string rfid_prob_map);
    ~PersonParticleFilter();

    void initUniform();
    void predict(double timeStep);
    void update(SensorData &obs_data);
    void resample();
    void setSensorModel(RfidSensorModel *model);
    double entropyParticles();
    double entropyGMM();

    void initFromParticles(sensor_msgs::PointCloud &particle_set);

protected:
    double sigma_pose_;             ///< Starndard deviation for person movement
    RfidSensorModel *rfid_model_;   ///< Probability model for RFID observations
    bool local_sensor_;             ///< True if the sensor model is created locally
    bool external_sensor_;          ///< True if an external sensor model is loaded
};


#endif
