#ifndef PERSON_PARTICLE_FILTER_H
#define PERSON_PARTICLE_FILTER_H

#include "geometry_msgs/PoseArray.h"
#include "particle_filter.h"

#include <vector>
using namespace std;

/** \brief Information for a single particle

*/
class PersonParticle : public Particle
{
public:
    // Pose represented by this sample
    vector<double> pose_;
    PersonParticle();
};

/**
  Class to implement a particle filter that estimate a person position
  */
class PersonParticleFilter : public ParticleFilter
{
public:
    PersonParticleFilter(int n_particles, nav_msgs::OccupancyGridConstPtr& map);

    void initUniform();
    void predict(double timeStep);
    void update(bool &rfid_mes, double &robot_x, double &robot_y);
    void update(bool &rfid_mes, geometry_msgs::PoseArray &robot_cloud);
};


#endif